import os
from pathlib import Path
import tempfile


def atomic_write(path: Path, content: bytes) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary: Path | None = None
	try:
		with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
			temporary = Path(output.name)
			output.write(content)
			output.flush()
			os.fsync(output.fileno())
		os.replace(temporary, path)
	finally:
		if temporary is not None:
			temporary.unlink(missing_ok=True)
