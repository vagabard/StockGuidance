from io import BytesIO
import json
from zipfile import ZIP_STORED, ZipFile

from stockguidance.artifacts.decoding import exact_mapping
from stockguidance.market_data.identity import encode_json


def archive_bytes(manifest: dict[str, object], files: dict[str, bytes]) -> bytes:
	output = BytesIO()
	with ZipFile(output, "w", compression=ZIP_STORED) as archive:
		archive.writestr("manifest.json", encode_json(manifest))
		for name, content in files.items():
			archive.writestr(name, content)
	return output.getvalue()


def read_archive(content: bytes) -> tuple[dict[str, object], dict[str, bytes]]:
	with ZipFile(BytesIO(content), "r") as archive:
		names = archive.namelist()
		if len(set(names)) != len(names) or "manifest.json" not in names or any(name.startswith(("/", "\\")) or ".." in name.split("/") for name in names):
			raise ValueError("Invalid inference archive layout")
		manifest = json.loads(archive.read("manifest.json"))
		fields = exact_mapping(manifest, ("schema", "artifact_id", "payload"))
		return fields, {name: archive.read(name) for name in names if name != "manifest.json"}
