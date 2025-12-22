from __future__ import annotations

import pathlib
import sys


# Ensure project root is importable for tests that access top-level modules.
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))
