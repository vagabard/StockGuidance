from __future__ import annotations

import os
import traceback


def resolve_manifest_path(path: str) -> str:
    """Resolve a manifest path using the new convention:
    - If a directory is provided, look for files that end with 'model_manifest.json'.
      If multiple are found, raise an error. If exactly one is found, use it.
    - If a weights file (.pt/.pth) is provided, search its directory the same way.
    - If a JSON file path is provided, use it directly.
    - Otherwise, return the path as-is.
    """

    def _find_manifest_in_dir(directory_to_search_for_manifest: str) -> str:
        try:
            candidates: list[str] = []
            list_of_files = os.listdir(directory_to_search_for_manifest)
            for test_file in list_of_files:
                if test_file.lower().endswith("model_manifest.json"):
                    candidates.append(os.path.join(directory_to_search_for_manifest, test_file))
        except FileNotFoundError as e:
            traceback.print_exception(e)
            candidates = []

        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(
                f"Multiple manifest files found in '{directory_to_search_for_manifest}': "
                + ", ".join(os.path.basename(c) for c in candidates)
            )
        # None found; return a conventional default path (will be validated by caller)
        return os.path.join(directory_to_search_for_manifest, "model_manifest.json")

    # If given a directory, search for '*model_manifest.json' inside it
    if os.path.isdir(path):
        return _find_manifest_in_dir(path)
    # If given a JSON file, assume it's the manifest
    if path.lower().endswith(".json"):
        return path
    # If given a weights file, search sibling directory for '*model_manifest.json'
    if path.lower().endswith(".pt") or path.lower().endswith(".pth"):
        d = os.path.dirname(os.path.abspath(path))
        return _find_manifest_in_dir(d)
    # Fallback: treat as manifest
    return path
