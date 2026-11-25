"""Install the validation gate and retire detailed delta evidence in scope."""

import argparse
from datetime import date
import json
from pathlib import Path

from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.simplified_runtime import source_from_database
from stockguidance.database.validation_gate import GATE_DDL, design_version
from stockguidance.workflows.market_lock import MaintenanceWriterLock


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--start-date", type=date.fromisoformat, required=True)
	parser.add_argument("--end-date", type=date.fromisoformat, required=True)
	parser.add_argument("--dbinfo-file", type=Path, default=Path("dbinfo.id"))
	args = parser.parse_args()
	connect = MySQLConnectionFactory()
	source, _ = source_from_database(args.dbinfo_file,Path("data"),connect)
	with MaintenanceWriterLock(source,connect,timeout_seconds=0) as lock:
		connection = connect(args.dbinfo_file)
		cursor = connection.cursor()
		try:
			cursor.execute(GATE_DDL)
			cursor.execute("SELECT id,transaction_date FROM transaction_dates WHERE transaction_date BETWEEN %s AND %s ORDER BY transaction_date", (args.start_date,args.end_date))
			for day_id,day in cursor.fetchall():
				last = 0
				while True:
					lock.assert_held()
					cursor.execute("SELECT ticker_id FROM daily_delta_stock_data WHERE date_id=%s AND ticker_id>%s ORDER BY ticker_id LIMIT 500", (day_id,last))
					ids = [row[0] for row in cursor.fetchall()]
					if not ids:
						break
					last = max(ids)
					cursor.execute("UPDATE daily_delta_stock_data SET provenance=JSON_ARRAY() WHERE date_id=%s AND ticker_id IN ("+",".join("%s" for _ in ids)+") AND JSON_LENGTH(provenance)>0", (day_id,*ids))
					connection.commit()
				print(json.dumps({"date":str(day),"detailed_delta_provenance_removed":True}),flush=True)
			# This table was introduced solely for the superseded compaction.
			cursor.execute("DROP TABLE IF EXISTS stockguidance_provenance_sets")
			connection.commit()
			print(json.dumps({"validation_gate_installed":True,"design_version":design_version()}),flush=True)
		finally:
			cursor.close()
			connection.close()


if __name__ == "__main__":
	main()
