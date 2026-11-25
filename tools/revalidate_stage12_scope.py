"""Independent stored-data checks for the bounded AAPL/MSFT scope."""

from datetime import date, timedelta, datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
from statistics import mean, pstdev

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.repositories.closes import CloseQuery
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.simplified_runtime import source_from_database, validate_simplified_database
from stockguidance.database.validation_gate import data_versions, design_version, record_raw_passes
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.identity import content_id
from stockguidance.workflows.simplified_readiness import SimplifiedCloseRepository


def validate(dbinfo: Path, start: date, end: date, output: Path) -> dict:
	connect = MySQLConnectionFactory()
	source, names = source_from_database(dbinfo, Path("data"), connect)
	calendar = ExchangeCalendar(source.calendar_id, start-timedelta(days=200), end+timedelta(days=14))
	targets = calendar.sessions(start,end)
	context = calendar.sessions(calendar.shift(targets[0],-61).session_date,end)
	validation = validate_simplified_database(source,connect,calendar,targets,table_names=names)
	if validation.state == "unfinished_operation":
		raise RuntimeError("Pending maintenance overlaps the validation scope")
	connection = connect(dbinfo)
	cursor = connection.cursor()
	findings = []
	try:
		cursor.execute("SELECT COUNT(*) FROM stockguidance_pending_updates")
		if cursor.fetchone()[0]:
			raise RuntimeError("Pending maintenance must finish before readiness validation")
		cursor.execute("SELECT id,label FROM ticker_symbol WHERE label IN ('AAPL','MSFT') ORDER BY label")
		tickers = cursor.fetchall()
		if len(tickers)!=2:
			raise RuntimeError("Missing AAPL/MSFT identities")
		cursor.execute("SELECT id,transaction_date,raw_complete,delta_complete FROM transaction_dates WHERE transaction_date BETWEEN %s AND %s", (context[0].session_date,end))
		sessions = {row[1]:row for row in cursor.fetchall()}
		cursor.execute("SELECT r.date_id,r.ticker_id,r.close,r.open,r.state_id FROM raw_stock_data r JOIN transaction_dates t ON t.id=r.date_id WHERE t.transaction_date BETWEEN %s AND %s AND r.ticker_id IN (%s,%s)", (context[0].session_date,end,tickers[0][0],tickers[1][0]))
		raw = {(row[0],row[1]):row[2:] for row in cursor.fetchall()}
		checks = 0
		for session in targets:
			row = sessions.get(session.session_date)
			if row is None or not(row[2] and row[3]):
				findings.append({"date":str(session.session_date),"reason":"incomplete_session"})
				continue
			day_id = row[0]
			for ticker,label in tickers:
				cursor.execute("SELECT close_ratio_to_prior,calculation_status FROM daily_delta_stock_data WHERE date_id=%s AND ticker_id=%s", (day_id,ticker))
				delta = cursor.fetchone()
				prior_id = sessions.get(calendar.shift(session,-1).session_date, (None,))[0]
				current = raw.get((day_id,ticker))
				previous = raw.get((prior_id,ticker))
				if not current or not previous or current[2]!=1 or previous[2]!=1 or not delta or delta[1] not in ('ready','not_yet_available') or delta[0] is None or not math.isclose(float(delta[0]),float(current[0]/previous[0]),rel_tol=1e-10,abs_tol=1e-12):
					findings.append({"date":str(session.session_date),"ticker":label,"reason":"delta_input_or_value"})
				for family in ('price','price_delta'):
					for window in (20,60):
						values = []
						for offset in range(-window+1,1):
							day = calendar.shift(session,offset)
							db = sessions.get(day.session_date)
							price = None if db is None or not db[2] else raw.get((db[0],ticker))
							if price is None or price[2]!=1 or price[0] is None:
								break
							value = price[0]
							if family=='price_delta':
								prev_db = sessions.get(calendar.shift(day,-1).session_date)
								prev = None if prev_db is None or not prev_db[2] else raw.get((prev_db[0],ticker))
								if prev is None or prev[2]!=1 or not prev[0]:
									break
								value = value/prev[0]-Decimal(1)
							values.append(float(value))
						cursor.execute(f"SELECT zscore,status FROM zscores_{family}_{window} WHERE date_id=%s AND ticker_id=%s", (day_id,ticker))
						stored = cursor.fetchone()
						if len(values)!=window or not stored or stored[1]!=0 or stored[0] is None or pstdev(values)==0 or not math.isclose(float(stored[0]), (values[-1]-mean(values))/pstdev(values), rel_tol=2e-5,abs_tol=2e-5):
							findings.append({"date":str(session.session_date),"ticker":label,"reason":"zscore_mismatch_or_unavailable","family":family,"window":window})
						checks += 1
		ids = [row[0] for row in sessions.values()]
		versions = data_versions(cursor,names,ids)
		cursor.execute("SELECT z.series_key,z.date_id,z.content_digest,z.completed_at FROM stockguidance_zscore_sessions z JOIN transaction_dates t ON t.id=z.date_id WHERE t.transaction_date BETWEEN %s AND %s ORDER BY z.date_id,z.series_key", (context[0].session_date,end))
		version = content_id({"raw":versions,"derived":[[str(v) for v in row] for row in cursor.fetchall()]})
	finally:
		cursor.close()
		connection.close()
	# Exercise the app's gated read with a historical cutoff, earlier than import.
	boundary = HistoryBoundary(source.identity,calendar.calendar_id,context[0].session_date,HistoryProvenance.SOURCE_HISTORY,"Current validated historical data",datetime.now(timezone.utc))
	query = CloseQuery(SessionQuery(boundary,targets,targets[-1].closes_at+timedelta(minutes=1)),tuple(row[0] for row in tickers))
	try:
		closes = SimplifiedCloseRepository(source,connect,names).read(query)
		if len(closes.observations)!=len(targets)*2 or closes.issues:
			findings.append({"reason":"application_close_coverage","observations":len(closes.observations),"issues":[str(issue) for issue in closes.issues]})
	except Exception as error:
		findings.append({"reason":"application_gate","error":str(error)})
	if not findings:
		record_raw_passes(source,connect,{sessions[session.session_date][0]:version for session in targets},phase="aapl-msft")
	report = {"start_date":str(start),"end_date":str(end),"tickers":[row[1] for row in tickers],"stored_coverage_complete":not findings,"data_version":version,"design_version":design_version(),"zscore_comparisons":checks,"findings":findings,"scope":"stored data and application close reads; model training/inference not executed"}
	output.write_text(json.dumps(report,indent=2),encoding="utf-8")
	return report
