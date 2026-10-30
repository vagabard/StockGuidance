from dataclasses import asdict
import random

from stockguidance.config.validation import require_positive_int
from stockguidance.datasets.sample import Sample
from stockguidance.datasets.training import TrainingDataset
from stockguidance.features.specification import FeatureSpecV2
from stockguidance.validation.sample_reporting import sample_payload


def select_samples(dataset: TrainingDataset, count: int, seed: int) -> tuple[Sample, ...]:
	require_positive_int(count, "diagnostic sample count")
	if type(seed) is not int or seed < 0:
		raise ValueError("Diagnostics require a nonnegative integer seed")
	selected = random.Random(seed).sample(list(dataset.samples), min(count, len(dataset.samples)))
	return tuple(sorted(selected, key=lambda sample: sample.timing.entry_session.sequence))


def inspect_features(dataset: TrainingDataset, count: int = 5, seed: int = 0) -> dict[str, object]:
	if not dataset.samples:
		channels: tuple[str, ...] = ()
	else:
		specification = dataset.samples[0].features.specification
		channels = specification.channel_keys if isinstance(specification, FeatureSpecV2) else tuple(item.value for item in specification.feature_order)
	return {"schema": "stockguidance-feature-inspection-1", "source": None if not dataset.samples else asdict(dataset.samples[0].source), "training_cutoff": dataset.training_cutoff.isoformat(), "seed": seed, "eligible_samples": len(dataset.eligible), "excluded_samples": len(dataset.excluded), "channels": channels, "samples": [sample_payload(sample, assess_features=True) | {"feature_identity": sample.feature_cache_key, "issues": [{"ticker_id": issue.ticker_id, "session": issue.session.session_date.isoformat(), "channel": getattr(issue, "channel", "close"), "state": issue.state.value} for issue in sample.features.issues]} for sample in select_samples(dataset, count, seed)]}
