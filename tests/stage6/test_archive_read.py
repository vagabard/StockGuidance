from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from stockguidance.cli.archive_study import result_parity, stratified_sample
from stockguidance.performance.archive_read import archive_records_id, read_archive, read_database, run_request


def write_file(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
	path = tmp_path / "2024-07-01.ndjson"
	path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
	return path


def record(**changes: object) -> dict[str, object]:
	row: dict[str, object] = {"T": "AAA", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100, "vw": None, "n": None, "t": int(datetime(2024, 7, 1, 20, tzinfo=timezone.utc).timestamp() * 1000)}
	row.update(changes)
	return row


def test_archive_hash_and_normalization(tmp_path: Path) -> None:
	path = write_file(tmp_path, [record(T="BBB"), record(c="10.12345")])
	bars, digest = read_archive(path)
	assert [item.ticker for item in bars] == ["AAA", "BBB"]
	assert str(bars[0].close) == "10.1235"
	assert bars[0].vwap is None
	assert digest == sha256(path.read_bytes()).hexdigest()
	assert len(archive_records_id(bars)) == 64


@pytest.mark.parametrize("rows", [[], [record(), record()], [record(t=0)], [record(v=True)], [record(v=-1)], [record(n=2**63)], [record(c=15)], [record(c="NaN")]])
def test_invalid_archive_is_rejected(tmp_path: Path, rows: list[dict[str, object]]) -> None:
	with pytest.raises(ValueError):
		read_archive(write_file(tmp_path, rows))


def test_duplicate_json_fields_rejected(tmp_path: Path) -> None:
	path = tmp_path / "2024-07-01.ndjson"
	path.write_text('{"T":"AAA","T":"BBB"}\n', encoding="utf-8")
	with pytest.raises(ValueError, match="Duplicate JSON"):
		read_archive(path)


def test_fractional_adjusted_volume_is_preserved(tmp_path: Path) -> None:
	bars, digest = read_archive(write_file(tmp_path, [record(v=14982.4)]))
	assert str(bars[0].volume) == "14982.4"
	assert archive_records_id(bars) != archive_records_id(read_archive(write_file(tmp_path, [record(v=14982)]))[0])


def test_large_adjusted_price_is_not_clipped_to_legacy_schema(tmp_path: Path) -> None:
	bars, digest = read_archive(write_file(tmp_path, [record(o="1702500000000000", h="1728750000000000", l="1522500000000000", c="1590000000000000")]))
	assert str(bars[0].open) == "1702500000000000.0000"


def test_parallel_and_serial_have_identical_results(tmp_path: Path) -> None:
	path = write_file(tmp_path, [record()])
	request = {"paths": [str(path)], "workers": 1, "mode": "files", "repeats": 2}
	serial = run_request(request)
	parallel = run_request(dict(request, workers=3))
	assert {measurement["digest"] for report in (serial, parallel) for measurement in report["measurements"]} == {serial["measurements"][0]["digest"]}


@pytest.mark.parametrize("workers", [0, 4, True])
def test_worker_limit(workers: int) -> None:
	with pytest.raises(ValueError):
		run_request({"paths": ["unused"], "workers": workers, "mode": "files"})


def test_stratified_sampling_is_reproducible() -> None:
	paths = tuple(Path(f"{index:03}.ndjson") for index in range(90))
	selected = stratified_sample(paths, 9, 20261019)
	assert selected == stratified_sample(paths, 9, 20261019)
	assert len(set(selected)) == 9
	assert [sum(lower <= int(path.stem) < lower + 30 for path in selected) for lower in (0, 30, 60)] == [3, 3, 3]


def test_cross_mode_archive_drift_blocks_benchmark_selection() -> None:
	runs = [{"mode": "files", "result": {"measurements": [{"digest": "file-mode", "sessions": [{"session": "2024-07-01", "archive_id": "before"}]}]}}, {"mode": "reconcile", "result": {"measurements": [{"digest": "reconcile-mode", "sessions": [{"session": "2024-07-01", "archive_id": "after"}]}]}}]
	assert not result_parity(runs)
	runs[1]["result"]["measurements"][0]["sessions"][0]["archive_id"] = "before"
	assert result_parity(runs)


def test_database_query_is_bounded_and_does_not_buffer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from types import SimpleNamespace
	from stockguidance.database.table_names import load_table_names
	monkeypatch.setattr("stockguidance.performance.archive_read.load_database_configuration", lambda path: SimpleNamespace(load_table_names=load_table_names))
	class Cursor:
		def __init__(self) -> None:
			self.queries: list[tuple[str, object]] = []
			self.rows: list[tuple[object, ...]] = []
			self.closed = False

		def execute(self, query: str, parameters: object) -> None:
			self.queries.append((query, parameters))
			self.rows = [(42,)] if "SELECT id" in query else [("AAA", 10, 12, 9, 11, 100, None, None)]

		def __iter__(self):
			return iter(self.rows)

		def fetchmany(self, size: int):
			batch, self.rows = self.rows[:size], self.rows[size:]
			return batch

		def close(self) -> None:
			self.closed = True
	from datetime import date
	cursor = Cursor()
	bars = read_database(SimpleNamespace(cursor=lambda: cursor), tmp_path / "unused", date(2024, 7, 1))
	assert cursor.closed
	assert "WHERE r.date_id = %s" in cursor.queries[1][0]
	assert cursor.queries[1][1] == (42,)
	assert bars[0].transactions is None


def test_worker_rolls_back_and_closes_on_parse_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from types import SimpleNamespace
	from stockguidance.performance.archive_read import run_chunk
	actions = []
	cursor = SimpleNamespace(execute=lambda *arguments: None, close=lambda: actions.append("cursor_close"))
	connection = SimpleNamespace(cursor=lambda: cursor, rollback=lambda: actions.append("rollback"), close=lambda: actions.append("connection_close"))
	monkeypatch.setattr("stockguidance.performance.archive_read.streaming_connection", lambda path: connection)
	path = write_file(tmp_path, [])
	with pytest.raises(ValueError):
		run_chunk(([str(path)], "reconcile", "unused"))
	assert actions == ["cursor_close", "rollback", "connection_close"]


def test_java_and_python_have_identical_real_contract(tmp_path: Path) -> None:
	from stockguidance.cli.compare_database_downloads import _classpath
	root = Path(__file__).resolve().parents[2]
	if shutil.which("java") is None or not (root / "StockGuidance/java/target/classes/stockguidance/validation/ArchiveStudyMain.class").exists():
		pytest.skip("Build the Java module before cross-language acceptance")
	path = write_file(tmp_path, [record(v=14982.4), record(T="BBB", o="1702500000000000", h="1728750000000000", l="1522500000000000", c="1590000000000000")])
	request = {"paths": [str(path)], "workers": 3, "mode": "files", "repeats": 1}
	request_file = tmp_path / "request.json"
	request_file.write_text(json.dumps(request), encoding="utf-8")
	completed = subprocess.run(["java", "-XX:ActiveProcessorCount=3", "-XX:+UseSerialGC", "-cp", _classpath(None), "stockguidance.validation.ArchiveStudyMain", str(request_file)], capture_output=True, text=True, timeout=30, check=True)
	java = json.loads(completed.stdout)
	python = run_request(dict(request, workers=1))
	assert java["measurements"][0]["sessions"] == python["measurements"][0]["sessions"]
	assert java["measurements"][0]["digest"] == python["measurements"][0]["digest"]
