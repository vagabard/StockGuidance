from dataclasses import asdict
import random

from stockguidance.config.validation import require_positive_int
from stockguidance.datasets.sample import Sample
from stockguidance.datasets.training import TrainingDataset
from stockguidance.validation.sample_reporting import sample_payload


def select_samples(dataset: TrainingDataset, count: int, seed: int) -> tuple[Sample, ...]:
	require_positive_int(count, "diagnostic sample count")
	if type(seed) is not int or seed < 0:
		raise ValueError("Diagnostics require a nonnegative integer seed")
	selected = random.Random(seed).sample(list(dataset.samples), min(count, len(dataset.samples)))
	return tuple(sorted(selected, key=lambda sample: sample.timing.entry_session.sequence))


def inspect_features(dataset: TrainingDataset, count: int = 5, seed: int = 0) -> dict[str, object]:
	return {"schema": "stockguidance-feature-inspection-1", "source": None if not dataset.samples else asdict(dataset.samples[0].source), "training_cutoff": dataset.training_cutoff.isoformat(), "seed": seed, "eligible_samples": len(dataset.eligible), "excluded_samples": len(dataset.excluded), "samples": [sample_payload(sample, assess_features=True) | {"feature_identity": sample.feature_cache_key, "issues": [{"ticker_id": issue.ticker_id, "session": issue.session.session_date.isoformat(), "state": issue.state.value} for issue in sample.features.issues]} for sample in select_samples(dataset, count, seed)]}
