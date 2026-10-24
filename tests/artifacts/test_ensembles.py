from dataclasses import replace

import pytest

from stockguidance.artifacts.ensembles import export_ensemble, load_ensemble
from stockguidance.artifacts.archive import archive_bytes, read_archive
from stockguidance.artifacts.exporting import export_artifact
from stockguidance.artifacts.loading import load_artifact
from stockguidance.features.specification import CloseFeature
from stockguidance.inference.prediction import predict_sample
from stockguidance.market_data.identity import content_id
from stockguidance.training.service import train_model


def test_ensemble_records_explicit_members_and_loads_without_original_paths(training, samples, tmp_path):
	dataset, evaluation, config = training
	first = train_model(dataset, config, tmp_path / "first").artifact
	second = train_model(dataset, replace(config, seed=7), tmp_path / "second").artifact
	inference = samples.build(9)
	expected = tuple(0.25 * left + 0.75 * right for left, right in zip(predict_sample(load_artifact(first), inference), predict_sample(load_artifact(second), inference)))
	reference = export_ensemble(tmp_path / "ensemble", (first, second), (0.25, 0.75))
	first.path.unlink()
	second.path.unlink()
	loaded = load_ensemble(reference)
	assert predict_sample(loaded, inference) == pytest.approx(expected, abs=1e-7)
	assert [member.artifact_id for member in loaded.members] == [first.artifact_id, second.artifact_id]
	assert loaded.weights == (0.25, 0.75)
	assert loaded.semantics.features.ticker_ids == (17, 3)


@pytest.mark.parametrize("mismatch", ["tickers", "transform", "adjustment", "calendar", "source"])
def test_ensemble_rejects_equal_dimension_semantic_mismatches(training, tmp_path, mismatch):
	dataset, evaluation, config = training
	first = train_model(dataset, config, tmp_path / "first").artifact
	loaded = load_artifact(first)
	semantics = loaded.metadata.semantics
	if mismatch == "tickers":
		semantics = replace(semantics, features=replace(semantics.features, ticker_ids=(3, 17)))
	elif mismatch == "transform":
		semantics = replace(semantics, features=replace(semantics.features, feature_order=(CloseFeature.LOG_CLOSE,)))
	elif mismatch == "adjustment":
		from stockguidance.config.source import PriceAdjustment
		semantics = replace(semantics, source=replace(semantics.source, adjustment=PriceAdjustment.UNADJUSTED), features=replace(semantics.features, adjustment=PriceAdjustment.UNADJUSTED))
	elif mismatch == "calendar":
		semantics = replace(semantics, calendar_version="fixture-other-calendar-version")
	else:
		semantics = replace(semantics, source=replace(semantics.source, universe="different-universe"))
	second = export_artifact(tmp_path / "second", loaded.model, replace(loaded.metadata, semantics=semantics))
	assert semantics.features.input_size == loaded.metadata.semantics.features.input_size
	with pytest.raises(ValueError, match="semantics"):
		export_ensemble(tmp_path / "bad", (first, second))
	assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("weights", [(1.0,), (-1.0, 2.0), (float("nan"), 0.5), (0.2, 0.3)])
def test_invalid_ensemble_weights_are_rejected(training, tmp_path, weights):
	dataset, evaluation, config = training
	first = train_model(dataset, config, tmp_path / "first").artifact
	second = train_model(dataset, replace(config, seed=9), tmp_path / "second").artifact
	with pytest.raises(ValueError):
		export_ensemble(tmp_path / "bad", (first, second), weights)
	assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("corruption", ["missing_member", "changed_identity", "invalid_weight"])
def test_corrupt_ensemble_members_and_metadata_are_rejected(training, tmp_path, corruption):
	dataset, evaluation, config = training
	first = train_model(dataset, config, tmp_path / "first").artifact
	second = train_model(dataset, replace(config, seed=3), tmp_path / "second").artifact
	reference = export_ensemble(tmp_path / "ensemble", (first, second))
	manifest, files = read_archive(reference.path.read_bytes())
	if corruption == "missing_member":
		del files["members/1.sgmodel"]
	elif corruption == "changed_identity":
		files["members/1.sgmodel"] = files["members/0.sgmodel"]
	else:
		manifest["payload"]["members"][0]["weight"] = -0.5
		manifest["artifact_id"] = content_id(manifest["payload"])
	path = tmp_path / "bad"
	path.write_bytes(archive_bytes(manifest, files))
	with pytest.raises(ValueError):
		load_ensemble(path)
