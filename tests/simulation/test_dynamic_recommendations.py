from dataclasses import replace
from types import SimpleNamespace

from stockguidance.features.construction import FeatureValues
from stockguidance.reporting.simulation import simulation_payload
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.replay import SavedExecutionPrices,SavedScoreSource
from stockguidance.simulation.score_source import FixedModelScores
from stockguidance.targets.construction import TargetValues


def test_universe_change_preserves_old_positions_and_saved_replay(simulation):
	underlying=FixedModelScores(simulation.reference,simulation.signals)
	class ChangingScores:
		dynamic_universe=True
		policy=underlying.policy
		training_runs=()
		def validate(self,request,first,calendar):
			underlying.validate(request,first,calendar)
		def forecast(self,request,timing,index):
			full=underlying.forecast(request,timing,index)
			# First buy selects 17, the next selects 3. The engine must retain
			# old quote coverage until positions and benchmarks have settled.
			chosen=17 if index==0 else 3
			spec=replace(full.sample.features.specification,ticker_ids=(chosen,))
			features=FeatureValues(spec,full.sample.features.sessions,(0.0,)*spec.input_size)
			sample=replace(full.sample,features=features,targets=TargetValues((None,),(None,)))
			prediction=replace(full.prediction,scores=tuple(s for s in full.prediction.scores if s.ticker_id==chosen),eligibility=tuple(s for s in full.prediction.eligibility if s.ticker_id==chosen),ranking=tuple(s for s in full.prediction.scores if s.ticker_id==chosen))
			return replace(full,sample=sample,prediction=prediction,resolution_metadata={"mode":"historical_replay"})
	result=simulation.run(source=ChangingScores())
	assert result.successful and result.complete
	assert result.forecasts[0].sample.features.specification.ticker_ids==(17,)
	assert result.forecasts[1].sample.features.specification.ticker_ids==(3,)
	assert any(trade.position.ticker_id==17 for trade in result.trades)
	report=simulation_payload(result,schema="stockguidance-simulation-2")
	assert report["dynamic_universe"] and not report["point_in_time_replay"]
	scores=SavedScoreSource(report,simulation.request,simulation.calendar)
	prices=SavedExecutionPrices.from_report(report,simulation.request.source,simulation.calendar,simulation.request.features.ticker_ids)
	replayed=simulate(simulation.request,simulation.samples.boundary,simulation.calendar,scores,prices)
	assert replayed.final==result.final
	assert [v.total_equity for v in replayed.valuations]==[v.total_equity for v in result.valuations]


def test_selector_only_runs_on_retraining_and_uses_each_decisions_previous_session(tmp_path):
	from datetime import date,timedelta
	from stockguidance.calendar.exchange import ExchangeCalendar
	from stockguidance.recommendations.configuration import RefreshMode
	from stockguidance.simulation.recommendation_source import RecommendationReplayScores
	from stockguidance.workflows.flexible_recommendation import RecommendationSetup
	from stockguidance.artifacts.contracts import ArtifactReference
	from stockguidance.calendar.decisions import DecisionTiming
	from stockguidance.portfolio.policy import AllocationSpec,AllocationPolicy
	from stockguidance.recommendations.eligibility import EligibilityPolicy
	calendar=ExchangeCalendar("XNYS",date(2026,10,1),date(2026,12,1))
	request=SimpleNamespace(allocation=AllocationSpec(AllocationPolicy.ALTERNATE_SESSIONS,calendar.session(date(2026,11,16))),top_k=2,decision_lead=timedelta(minutes=30),eligibility=EligibilityPolicy(0))
	resolved=[]
	def resolver(setup,options):
		resolved.append(setup.end)
		entry=calendar.shift(calendar.session(date.fromisoformat(setup.end)),1)
		return SimpleNamespace(entry_date=entry.session_date),options
	# Use real dataclasses for replace(), but a small fake runner to isolate
	# the cadence contract from model fitting and external state.
	from dataclasses import dataclass
	@dataclass
	class Config:
		entry_date:date
		end:date|None=None
		retraining:object=None
		replay_cutoff:object=None
		resolution_metadata:dict|None=None
	def typed_resolver(setup,options):
		value,opts=resolver(setup,options)
		return Config(value.entry_date,resolution_metadata={"data_end_session":setup.end}),opts
	def runner(config,options):
		entry=calendar.session(config.entry_date)
		timing=DecisionTiming(calendar.shift(entry,-1),entry,calendar.shift(entry,1),config.replay_cutoff)
		training=None if options.policy.value=="current" else SimpleNamespace(artifact=ArtifactReference(tmp_path/f"{entry.session_date}.sgmodel","a"*64))
		return SimpleNamespace(sample=SimpleNamespace(timing=timing),training=training,scores=(),eligibility=(),recommendation=SimpleNamespace(ranking=(),data_revision="r",artifact_id="a"*64),member_artifact_ids=(),member_weights=(),resolution_metadata=config.resolution_metadata)
	source=RecommendationReplayScores(RecommendationSetup("-1y"),tmp_path,every_buy_decisions=2,resolver=typed_resolver,runner=runner)
	for index,day in enumerate((16,18,20)):
		entry=calendar.session(date(2026,11,day))
		timing=DecisionTiming(calendar.shift(entry,-1),entry,calendar.shift(entry,1),entry.closes_at-timedelta(minutes=30))
		source.forecast(request,timing,index)
	assert resolved==["2026-11-13","2026-11-19"]
	assert len(source.training_runs)==2
