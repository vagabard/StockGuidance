import json


def test_training_job_carries_explicit_target_and_preprocessing(job, tmp_path) -> None:
	from stockguidance.preprocessing import NormalizationMethod
	from stockguidance.targets.specification import TargetKind
	from stockguidance.training.jobs import load_job

	job["target"] = {"kind": "return_zscore_per_ticker", "window_sessions": 20}
	job["preprocessing"] = {"input": "standard", "output": "min_max"}
	path = tmp_path / "stage6-job.json"
	path.write_text(json.dumps(job), encoding="utf-8")
	parsed = load_job(path)
	assert parsed.target.kind is TargetKind.RETURN_ZSCORE_PER_TICKER
	assert parsed.target.rolling_window_sessions == 20
	assert parsed.preprocessing.input_method is NormalizationMethod.STANDARD
	assert parsed.preprocessing.output_method is NormalizationMethod.MIN_MAX
