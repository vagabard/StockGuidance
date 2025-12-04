import os
import re
import shutil
from typing import Iterable


TIMESTAMP_DIR_RE = re.compile(r"^\d{8}_\d{6}$")

# Matches "rep1", "_rep2_", "acttanh_rep3_model" etc. The first group
# is the numeric repetition index.
REP_IN_NAME_RE = re.compile(r"rep(\d+)")


def _iter_timestamp_dirs(root: str) -> Iterable[str]:
    """Yield absolute paths of timestamp-looking subdirectories under root."""

    for name in os.listdir(root):
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        if not TIMESTAMP_DIR_RE.match(name):
            continue
        yield full


def _find_rep_index(filename: str) -> int | None:
    """Return the repetition index encoded in *filename* or None.

    The current convention uses fragments like "_rep1_", "_rep2_" etc., but
    this helper is intentionally a bit more permissive and simply searches for
    the first occurrence of "rep<digits>" anywhere in the name.
    """

    m = REP_IN_NAME_RE.search(filename)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:  # extremely unlikely, but keep it safe
        return None


def organize_rep_runs(
    data_root: str = os.path.join(os.path.dirname(__file__), "data"),
    dry_run: bool = False,
) -> None:
    """Group per-repetition output files into repN subdirectories.

    This scans timestamp-named directories directly under *data_root* (for
    example ``20251124_031117``) and, within each such directory, looks for
    files whose names contain a fragment like ``rep1``, ``rep2`` and so on.

    For every file that matches, a corresponding subdirectory named
    ``rep<index>`` is created (if needed) and the file is moved into it.

    The function is careful not to touch existing ``rep*`` directories and
    leaves files without a ``rep<digits>`` fragment in place.

    Parameters
    ----------
    data_root:
        Path to the directory that contains timestamped run subdirectories.
        By default this is ``<repo_root>/data``.
    dry_run:
        If True, just print what would be moved without changing anything on
        disk.
    """

    print(f"Scanning data root: {data_root}")
    if not os.path.isdir(data_root):
        print("Nothing to do: data root does not exist.")
        return

    for ts_dir in _iter_timestamp_dirs(data_root):
        ts_name = os.path.basename(ts_dir)
        print(f"\n[run {ts_name}] scanning for rep* files...")

        entries = os.listdir(ts_dir)
        # First, ignore existing rep* subdirectories so we don't descend into
        # them or attempt to move them.
        existing_rep_dirs = {
            name
            for name in entries
            if name.lower().startswith("rep")
            and os.path.isdir(os.path.join(ts_dir, name))
        }

        if existing_rep_dirs:
            print(
                "  Found existing rep directories: "
                + ", ".join(sorted(existing_rep_dirs))
            )

        for name in entries:
            src = os.path.join(ts_dir, name)

            # Skip directories entirely (we never move directories).
            if os.path.isdir(src):
                continue

            rep_index = _find_rep_index(name)
            if rep_index is None:
                continue

            dest_dir_name = f"rep{rep_index}"
            dest_dir = os.path.join(ts_dir, dest_dir_name)
            dest_path = os.path.join(dest_dir, name)

            print(f"  {name} -> {dest_dir_name}/{name}")

            if dry_run:
                continue

            os.makedirs(dest_dir, exist_ok=True)
            # If the file is already in the correct place, skip.
            if os.path.abspath(src) == os.path.abspath(dest_path):
                continue
            shutil.move(src, dest_path)


if __name__ == "__main__":  # pragma: no cover - convenience CLI
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Group files with repN in their name into repN subdirectories "
            "under each timestamped run directory in the data folder."
        )
    )
    parser.add_argument(
        "--data-root",
        default=os.path.join(os.path.dirname(__file__), "data"),
        help=(
            "Path to the data directory containing timestamped run "
            "subdirectories (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned moves without actually moving any files.",
    )

    args = parser.parse_args()
    organize_rep_runs(data_root=args.data_root, dry_run=args.dry_run)
