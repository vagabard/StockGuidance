import sys
from pathlib import Path

# Add project root to sys.path
root_path = Path(__file__).resolve().parent.parent.parent
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from viewers.examine_models_viewer import _run_as_script

if __name__ == "__main__":
    _run_as_script()
