import os
import re
import json
import sys
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, List, Tuple, Optional
from pathlib import Path


def get_preferences_path() -> Path:
	"""Return the path to the preferences JSON file in the cache directory."""
	# Path to this file is viewers/compare_runs_shared.py.
	# Root is the parent of viewers/.
	root = Path(__file__).resolve().parent.parent
	cache_dir = root / "cache"
	cache_dir.mkdir(exist_ok=True)
	return cache_dir / "viewer_preferences.json"


def load_preferences() -> Dict[str, Any]:
	"""Load preferences from disk."""
	path = get_preferences_path()
	if path.exists():
		try:
			with open(path, "r") as f:
				return json.load(f)
		except Exception:
			pass
	return {}


def save_preference(key: str, value: Any) -> None:
	"""Save a single preference to disk."""
	prefs = load_preferences()
	prefs[key] = value
	path = get_preferences_path()
	try:
		with open(path, "w") as f:
			json.dump(prefs, f, indent=2)
	except Exception:
		pass


def _is_timestamp_dir(name: str) -> bool:
	"""Return ``True`` if *name* looks like a YYYYMMDD_HHMMSS timestamp.

	This helper is used by :func:`scan_runs` and therefore shared between the
	different viewers.
	"""

	return re.fullmatch(r"\d{8}_\d{6}", name or "") is not None


def has_any_simulation_files(path: str) -> bool:
	"""Return True if ``path`` contains any end or epoch stock_simulation CSVS or recommendations.txt."""
	try:
		if not path or not os.path.isdir(path):
			return False
		# Quick heuristics without loading CSVs fully
		names = set(os.listdir(path))
	except Exception:
		return False

	for n in names:
		if re.match(r"stock_simulation_(?:ensemble_)?\d+(?:days|percent)?(?:_epoch_\d+)?\.csv$", n):
			return True
		if n.startswith("recommendations-") and n.endswith(".txt"):
			return True
	return False


def _safe_cast(value: str) -> Any:
	"""Best‑effort cast from a ``runParameters.txt`` value to a Python type.

	This helper is retained only for **backwards compatibility** with any
	legacy runs that still contain ``runParameters.txt`` files. Newer runs
	use JSON metadata (``family_state.json`` + ``single_run_0.json``) and do
	not rely on this parser.
	"""

	s = (value or "").strip()
	if s == "None":
		return None
	if s in ("True", "False"):
		return s == "True"
	try:
		if s and not any(c in s for c in ".,[]{} "):
			return int(s)
	except Exception:
		pass
	try:
		return float(s)
	except Exception:
		pass
	try:
		if s.startswith("[") and s.endswith("]"):
			return json.loads(s)
		if s.startswith("{") and s.endswith("}"):
			return json.loads(s)
	except Exception:
		pass
	if "," in s and "[" not in s and "]" not in s:
		parts = [p.strip() for p in s.split(",") if p.strip() != ""]
		casted = []
		for p in parts:
			casted.append(_safe_cast(p))
		return casted
	return s


def parse_run_parameters(params_path: str) -> Dict[str, Any]:
	"""Parse a ``runParameters.txt`` file into a ``dict``.

	The format is the one produced by the training scripts in this project
	(header lines followed by indented ``key: value`` pairs).  Values are
	passed through :func:`_safe_cast`.
	"""

	data: Dict[str, Any] = {}
	if not os.path.exists(params_path):
		return data
	try:
		with open(params_path, "r", encoding="utf-8") as f:
			lines = f.read().splitlines()
		for line in lines:
			if not line.strip():
				continue
			if line.startswith("Run Timestamp:"):
				ts = line.split(":", 1)[1].strip()
				data["run_timestamp"] = ts
				continue
			if line.startswith("Parameters:"):
				continue
			if ":" in line:
				stripped = line.strip()
				if ":" in stripped:
					k, v = stripped.split(":", 1)
					data[k.strip()] = _safe_cast(v.strip())
	except Exception as e:  # pragma: no cover - defensive
		data["_parse_error"] = str(e)
	return data


def _params_from_single_run_json(ts_path: str) -> Dict[str, Any]:
	"""Load parameters for a timestamp directory via ``single_run_0.json``.

	Newer training runs persist their configuration as a JSON pair of
	``FamilyRun`` + ``SingleRun`` (see :mod:`trainer.state.single_run`).
	Instead of re‑parsing ad‑hoc text files, we reuse those loaders and then
	project the most relevant fields into a flat parameter dictionary that
	``compare_runs_viewer`` and friends can consume.
	"""

	json_path = os.path.join(ts_path, "single_run_0.json")
	if not os.path.exists(json_path):
		return {}

	try:
		# Local import to avoid making ``compare_runs_shared`` unimportable in
		# environments where the training code is optional.
		from trainer.state.single_run import SingleRun  # type: ignore

		single_run = SingleRun.load(json_path)
	except Exception as e:  # pragma: no cover - defensive
		return {"_parse_error": f"single_run_0.json: {e}"}

	params: Dict[str, Any] = {"run_timestamp": getattr(single_run, "timestamp_id", None),
	                          "timestamp_id": getattr(single_run, "timestamp_id", None),
	                          "day_change_centered": getattr(single_run, "day_change_centered", None),
	                          "prior_days": getattr(single_run, "prior_days", None),
	                          "mlp_layers": getattr(single_run, "mlp_layers", None),
	                          "lr": getattr(single_run, "lr", None),
	                          "activation": getattr(single_run, "activation", None),
	                          "final_activation": getattr(single_run, "final_activation", None)}

	# Core identifying fields
	include_features = getattr(single_run, "include_features", None)
	if include_features is not None:
		params["include_features"] = list(include_features)

	# Attach FamilyRun‑level information that is relevant for grouping and
	# for high‑level training configuration.
	family_run = getattr(single_run, "single_run", None)
	if family_run is not None:
		for name in [
			"precision",
			"total_epochs_to_train",
			"train_percent",
			"val_percent",
			"simulate_top_k",
			"simulate_prior_only",
			"split_mode",
			"raw_train_start_with_special_characters",
			"raw_train_end_with_special_characters",
			"run_repeats",
			"neural_network_type",
			"hidden_layer_size",
		]:
			if hasattr(family_run, name):
				params[name] = getattr(family_run, name)

	return params


def scan_runs(base_dir: str) -> List[Tuple[str, Dict[str, Any]]]:
	"""Return ``(run_dir_name, params_dict)`` pairs discovered under *base_dir*.

	This is the factored‑out version of the logic used by
	``compare_runs_viewer`` so that multiple viewers can share the same
	filesystem convention:

	* each timestamp directory (``YYYYMMDD_HHMMSS``) may contain ``rep*``
	  subdirectories, each with its own ``runParameters.txt``;
	* if no ``rep*`` subdirectories are present, the timestamp directory
	  itself is treated as a single run.
	"""

	runs: List[Tuple[str, Dict[str, Any]]] = []
	if not os.path.isdir(base_dir):
		return runs

	def _scan_timestamp_dir(parent: str, ts_name: str, prefix: str = "") -> None:
		"""Scan a single timestamp directory for runs.

		*parent* is the directory that directly contains *ts_name*.
		If *prefix* is non-empty it is prepended to the returned ``_dir`` value so
		that callers can distinguish runs that live inside a ``run_family_*``
		container from legacy top-level runs.
		"""

		ts_path = os.path.join(parent, ts_name)
		if not os.path.isdir(ts_path):
			return

		try:
			entries = os.listdir(ts_path)
		except OSError:
			return

		# Prefer the new JSON metadata when available.
		base_params = _params_from_single_run_json(ts_path)

		rep_subdirs = [
				e
				for e in entries
				if e.lower().startswith("rep")
				and os.path.isdir(os.path.join(ts_path, e))
			]

		if rep_subdirs:
			# When rep* subdirectories exist we treat each rep as an individual
			# run but they all inherit the same configuration parameters from
			# ``single_run_0.json`` (or, for legacy runs, from runParameters.txt).
			legacy_parent_params: Dict[str, Any] | None = None
			if not base_params:
				# Backwards‑compatibility path for older runs that still used
				# text-based ``runParameters.txt`` files.
				parent_rp = os.path.join(ts_path, "runParameters.txt")
				if os.path.exists(parent_rp):
					try:
						legacy_parent_params = parse_run_parameters(parent_rp)
					except Exception:  # pragma: no cover - defensive
						legacy_parent_params = None

			for rep_name in sorted(rep_subdirs):
				rep_path = os.path.join(ts_path, rep_name)
				rel = f"{ts_name}/{rep_name}"
				if prefix:
					rel = f"{prefix}/{rel}"

				params: Dict[str, Any]
				if base_params:
					params = dict(base_params)
				else:
					# Legacy per‑rep override via runParameters.txt inside rep.
					rp = os.path.join(rep_path, "runParameters.txt")
					if os.path.exists(rp):
						params = parse_run_parameters(rp)
					elif legacy_parent_params is not None:
						params = dict(legacy_parent_params)
					else:
						continue

				params["_dir"] = rel
				params.setdefault("_ts", ts_name)
				if prefix:
					params["_family"] = prefix
				runs.append((rel, params))
		else:
			if not base_params:
				# Legacy, non‑JSON single run folder containing only a
				# ``runParameters.txt`` file.
				rp = os.path.join(ts_path, "runParameters.txt")
				if not os.path.exists(rp):
					return
				params = parse_run_parameters(rp)
			else:
				params = dict(base_params)

			rel = ts_name if not prefix else f"{prefix}/{ts_name}"
			params["_dir"] = rel
			params.setdefault("_ts", ts_name)
			if prefix:
				params["_family"] = prefix
			runs.append((rel, params))

	try:
		for entry in os.listdir(base_dir):
			path = os.path.join(base_dir, entry)
			if not os.path.isdir(path):
				continue

			# Legacy behaviour: top-level timestamp directories directly under
			# ``base_dir``.
			if _is_timestamp_dir(entry):
				_scan_timestamp_dir(base_dir, entry)
				continue

			# New behaviour: any subdirectory that is NOT a timestamp is treated
			# as a run-family container that may contain timestamp directories.
			# Each such child timestamp directory is scanned using the same logic
			# as top-level timestamps, but its ``_dir`` value is prefixed with
			# the family name so callers can distinguish them.
			family_name = entry
			try:
				for ts_name in os.listdir(path):
					if not _is_timestamp_dir(ts_name):
						continue
					_scan_timestamp_dir(path, ts_name, prefix=family_name)
			except OSError:
				continue
	except Exception:  # pragma: no cover - robust against partial trees
		return sorted(runs, key=lambda t: t[0])

	return sorted(runs, key=lambda t: t[0])


def compute_common_params(selected: List[Dict[str, Any]]) -> Dict[str, Any]:
	"""Return the subset of parameters that are identical across *selected*.

	``selected`` is expected to be a list of per‑run parameter dictionaries as
	produced by :func:`scan_runs` / :func:`parse_run_parameters`.
	"""

	if not selected:
		return {}

	# Work with the intersection of keys that are present in *all* runs so
	# that we only ever label parameters as "common" when they are actually
	# defined everywhere.
	keys = set.intersection(*(set(r.keys()) for r in selected)) if selected else set()

	common: Dict[str, Any] = {}
	for k in keys:
		# Values coming from ``runParameters.txt`` (and from derived
		# statistics added by viewers) may be lists, dictionaries, or other
		# unhashable types.  Using them directly in a ``set`` will raise
		# ``TypeError: unhashable type``.  To robustly compare values across
		# runs we therefore normalise each one into a JSON string
		# representation, mirroring the approach used by the more advanced
		# ``_compute_common_params`` helper in ``compare_runs_viewer.py``.
		try:
			serialised_vals = {
				json.dumps(r.get(k), sort_keys=True, default=str) for r in selected
			}
		except TypeError:
			# As a last‑ditch fallback, compare by ``str()`` conversion so we
			# never blow up on exotic objects.
			serialised_vals = {str(r.get(k)) for r in selected}

		if len(serialised_vals) == 1:
			# All runs agree on this parameter after normalisation; expose the
			# original (un‑normalised) value from the first run so callers get
			# back something that is convenient to display in the UI.
			common[k] = selected[0].get(k)

	return common


def runs_to_dataframe_dicts(runs: List[Dict[str, Any]], exclude_keys: List[str]) -> List[Dict[str, Any]]:
	"""Convert parameter dictionaries to row dictionaries for ``pandas``.

	This mirrors the behaviour of ``_to_dataframe_dicts`` in the original
	viewer but lives here so multiple viewers can share the same table
	representation.
	"""

	rows: List[Dict[str, Any]] = []
	for r in runs:
		row: Dict[str, Any] = {k: v for k, v in r.items() if k not in exclude_keys}
		row = {
			"run": r.get("_ts") or r.get("_dir"),
			"run_timestamp": r.get("run_timestamp"),
		} | row
		rows.append(row)
	return rows


def group_runs_by_timestamp(runs: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
	"""Group ``(run_name, params)`` pairs by their timestamp identifier.

	The returned mapping is used by both viewers to drive sidebar selection
	controls and to ensure that timestamp‑level operations (like selecting a
	range of runs) behave consistently.
	"""

	ts_to_runs: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
	for name, params in runs:
		ts_name = params.get("_ts")
		if not ts_name:
			dname = params.get("_dir") or name
			parts = re.split(r'[\\/]', str(dname))
			ts_name = parts[0] if parts else str(dname)
		ts_to_runs.setdefault(str(ts_name), []).append((name, params))
	return ts_to_runs


def build_timestamp_list(ts_to_runs: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> List[str]:
	"""Return timestamp keys ordered newest‑to‑oldest for UI drop‑downs.

	Timestamp directories follow the ``YYYYMMDD_HHMMSS`` pattern, so a simple
	reverse lexicographical sort yields descending chronological order.  This
	ensures that Start/End selectors in viewers list the latest runs first.
	"""

	return sorted(ts_to_runs.keys(), reverse=True)


def compute_selected_timestamps(
	start: str,
	end: str,
	start_family: str,
	end_family: str,
	timestamp_names: List[str],
	family_names: List[str],
	family_to_ts: Dict[str, List[str]],
) -> List[str]:
	"""Expand Start/End run and run‑family selections into timestamps.

	This helper encapsulates the common logic used by the Streamlit viewers
	for interpreting the "Start run", "End run", "Start run family" and
	"End run family" selectors.

	Behaviour mirrors the original implementations:

	* If both ``start`` and ``end`` are set to concrete timestamp names
	  (not ``"(none)"``), the inclusive range between them in
	  ``timestamp_names`` is returned.
	* Otherwise, if both ``start_family`` and ``end_family`` are set (and
	  ``family_names`` is non‑empty), we expand the selected families into
	  their member timestamps using ``family_to_ts`` and return the
	  de‑duplicated list in selection order.
	* If neither condition is met, an empty list is returned.
	"""

	# First preference: explicit Start/End run range.
	selected_ts: List[str] = []
	if start != "(none)" and end != "(none)" and timestamp_names:
		try:
			i0 = timestamp_names.index(start)
			i1 = timestamp_names.index(end)
		except ValueError:
			i0, i1 = 0, len(timestamp_names) - 1
		if i0 > i1:
			i0, i1 = i1, i0
		selected_ts = timestamp_names[i0 : i1 + 1]

	# Second preference: Start/End run‑family range, expanded to member
	# timestamps when no explicit run range is configured.
	if (not selected_ts) and start_family != "(none)" and end_family != "(none)" and family_names:
		try:
			fi0 = family_names.index(start_family)
			fi1 = family_names.index(end_family)
		except ValueError:
			fi0, fi1 = 0, len(family_names) - 1
		if fi0 > fi1:
			fi0, fi1 = fi1, fi0
		selected_families = family_names[fi0 : fi1 + 1]
		seen_ts: set[str] = set()
		expanded: List[str] = []
		for fam in selected_families:
			for ts_name in family_to_ts.get(fam, []):
				if ts_name not in seen_ts:
					expanded.append(ts_name)
					seen_ts.add(ts_name)
		selected_ts = expanded

	return selected_ts


def _normalise_family_params(params: Dict[str, Any]) -> str:
	"""Return a JSON normalised signature for *params* used in families.

	The goal is to detect whether a given ``initial_run`` family truly
	represents *multiple distinct parameter sets* or merely multiple
	repetitions (``rep`` sub‑runs) of an identical configuration.

	We therefore ignore purely metadata keys that should not contribute to
	the logical hyper‑parameter identity, such as the run directory or
	timestamp.  All remaining keys are serialised via ``json.dumps`` with a
	stable ordering so that semantically equal dictionaries yield identical
	signatures.
	"""

	# Keys that are treated as metadata and not part of the logical
	# hyper‑parameter set for a family.  ``initial_run`` itself is included
	# here because it is used as the *identifier* for the family rather than
	# a tunable parameter.
	meta_keys = {"_dir", "_ts", "_parse_error", "run_timestamp", "initial_run"}

	payload: Dict[str, Any] = {
		k: v for k, v in params.items() if k not in meta_keys
	}

	try:
		return json.dumps(payload, sort_keys=True, default=str)
	except TypeError:
		# Fall back to a lossy but robust representation that still groups
		# runs with the same stringified parameters together.
		return json.dumps({k: str(v) for k, v in payload.items()}, sort_keys=True)


def build_run_families(
	runs: List[Tuple[str, Dict[str, Any]]],
	*,
	require_multiple_param_sets: bool = True,
) -> Tuple[Dict[str, List[str]], List[str]]:
	"""Group runs into *families* based on the first subdirectory level.

	Parameters
	----------
	runs:
		List of ``(run_name, params)`` pairs as produced by :func:`scan_runs`.

	require_multiple_param_sets:
		When ``True`` (the default), only include families that exhibit more
		than one distinct *parameter set* when looking at the underlying
		``runParameters`` contents.

	Returns
	-------
	family_to_ts, family_names
		``family_to_ts`` maps each family identifier (the first subdirectory
		under the data folder) to the list of timestamp names that contain
		runs from that family.  ``family_names`` is a newest‑first list of
		the same identifiers, suitable for use as a drop‑down options list.
	"""

	# Build a mapping from family id -> {timestamps, param_signatures}.
	# The family id is taken from the ``_family`` parameter (the first
	# subdirectory under the data folder).
	raw_family_to_ts: Dict[str, List[str]] = {}
	family_param_sigs: Dict[str, set[str]] = {}

	for name, params in runs:
		fam_key = params.get("_family")
		if not fam_key:
			continue

		ts_name = params.get("_ts")
		if not ts_name:
			rel_dir = params.get("_dir") or name
			parts = re.split(r'[\\/]', str(rel_dir))
			ts_name = parts[1] if len(parts) > 1 and _is_timestamp_dir(parts[1]) else parts[0]

		ts_str = str(ts_name)

		bucket = raw_family_to_ts.setdefault(fam_key, [])
		if ts_str not in bucket:
			bucket.append(ts_str)

		sig_set = family_param_sigs.setdefault(fam_key, set())
		sig_set.add(_normalise_family_params(params))

	# Filter down to families that represent true *variations* in
	# run parameters if requested.
	family_to_ts: Dict[str, List[str]] = {}
	for fam_key, ts_list in raw_family_to_ts.items():
		sigs = family_param_sigs.get(fam_key) or set()
		if (not require_multiple_param_sets) or (len(sigs) > 1):
			family_to_ts[fam_key] = [str(ts) for ts in ts_list]

	# Sort family identifiers newest‑first.
	family_names: List[str] = sorted(family_to_ts.keys(), reverse=True)

	return family_to_ts, family_names


def plot_lines(
	plot_df,
	x_enc: str,
	height: int = 260,
	y_ref: float | None = None,
	log_y: bool = False,
	watermark_text: str | None = None,
	key: str | None = None,
):  # type: ignore[override]
	"""Shared small plotting helper for viewers.

	``plot_df`` is a tidy pandas DataFrame with one column designated for the
	x‑axis and all remaining columns treated as separate numeric series.

	The implementation is intentionally basic and self‑contained so that it
	can be used as a fallback when Altair is unavailable.  It mirrors the
	behaviour of ``_plot_lines`` in the original compare_runs_viewer.
	"""

	try:  # pragma: no cover - plotting is hard to test automatically
		import streamlit as st  # type: ignore
		import plotly.graph_objects as go  # type: ignore

		fig = go.Figure()
		for col in plot_df.columns:
			if col == x_enc:
				continue
			try:
				fig.add_trace(
					go.Scatter(x=plot_df[x_enc], y=plot_df[col], mode="lines", name=str(col))
				)
			except Exception:
				continue

		if y_ref is not None:
			try:
				fig.add_hline(y=y_ref, line_dash="dash", line_color="gray")
			except Exception:
				pass

		# Optionally switch to a logarithmic Y‑axis. This is controlled by
		# the caller so that viewers can expose a simple checkbox to toggle
		# between linear and log scaling.
		if log_y:
			try:
				fig.update_yaxes(type="log")
			except Exception:
				pass

		# Optional watermark overlay (e.g., mark placeholder/fake data)
		if watermark_text:
			try:
				fig.add_annotation(
					text=str(watermark_text),
					xref="paper",
					yref="paper",
					x=0.5,
					y=0.5,
					showarrow=False,
					font=dict(size=48, color="rgba(200,0,0,0.18)", family="Arial Black"),
					align="center",
					opacity=0.18,
				)
			except Exception:
				pass

		fig.update_layout(height=height, margin=dict(l=40, r=20, t=30, b=40))
		# NOTE: Streamlit auto-generates element IDs for Plotly charts based on the
		# figure + params. When many similar charts are rendered (e.g., in loops or
		# placeholder-data scenarios), IDs can collide unless we pass an explicit key.
		st.plotly_chart(fig, use_container_width=True, key=key)
	except Exception as e:
		print(f"Plotly error in plot_lines: {e}")
		# As an ultra‑simple fallback, show the data table so that the user at
		# least sees the numeric values even if plotting backends are missing.
		try:
			import streamlit as st  # type: ignore

			st.caption("Plotly missing; using native Streamlit chart.")
			st.line_chart(plot_df.set_index(x_enc))
		except Exception:
			try:
				st.dataframe(plot_df.set_index(x_enc), width="stretch")
			except Exception as e2:
				traceback.print_exception(e2)
				# Last‑ditch: print to stderr.
				#debug(f"plot_lines fallback: unable to display plot or dataframe: {e2}")



def render_common_params(
	common: Dict[str, Any],
	*,
	title: str = "Common parameters across runs",
	expanded: bool = False,
) -> None:
	"""Render a shared "common parameters" expander in Streamlit.

	Both viewers show a JSON blob of parameters that are identical across the
	currently scoped runs. Keeping the display consistent avoids subtle UI drift.

	The function is safe to import in non-Streamlit contexts because it imports
	Streamlit lazily.
	"""
	try:  # pragma: no cover - UI rendering
		import streamlit as st  # type: ignore
	except Exception:
		return

	with st.expander(title, expanded=expanded):
		if not common:
			st.caption("No common attributes across the current selection.")
		else:
			st.json(common)


def sidebar_family_and_build_selector(
	*,
	timestamp_names: List[str],
	family_names: List[str],
	family_to_ts: Dict[str, List[str]],
	key_prefix: str,
	default_select_all_builds: bool,
	empty_builds_means_all: bool = True,
	on_runs_updated: Callable[[List[str]], None] | None = None,
	caption: str | None = None,
) -> Tuple[List[str], List[str], List[str]]:
	"""Shared sidebar selector for (families, builds/timestamps).

	Requirements addressed:
	- select one or more run families
	- select one or more individual runs (timestamps)
	- shared behavior across viewers

	Behavior:
	- If *no families* are selected, the selector behaves as "all families".
	- If *no builds* are selected, the selector behaves as "all builds".

	Returns
	-------
	selected_builds, selected_families, available_builds
	"""
	try:  # pragma: no cover - UI rendering
		import streamlit as st  # type: ignore
	except Exception:
		# Non-Streamlit context: fall back to selecting everything.
		return list(timestamp_names), [], list(timestamp_names)

	if caption:
		st.sidebar.caption(caption)

	# --- Family selection (multi) ---
	fam_key = f"{key_prefix}_families"
	prev_fams = st.session_state.get(fam_key)
	if not isinstance(prev_fams, list):
		prev_fams = []
	prev_fams = [f for f in prev_fams if f in family_names]
	selected_families = st.sidebar.multiselect(
		"Run families",
		options=family_names,
		default=prev_fams,
		key=fam_key,
		help=(
			"Select one or more families. If none are selected, all families are included."
		),
	)

	# --- Builds selection (multi) ---
	if selected_families:
		# Preserve order as per family_names iteration; de-duplicate timestamps.
		seen: set[str] = set()
		available_builds: List[str] = []
		for fam in selected_families:
			for ts in family_to_ts.get(fam, []):
				ts_s = str(ts)
				if ts_s not in seen:
					available_builds.append(ts_s)
					seen.add(ts_s)
	else:
		available_builds = list(timestamp_names)

	builds_key = f"{key_prefix}_builds"
	prev_builds = st.session_state.get(builds_key)
	if not isinstance(prev_builds, list):
		prev_builds = []
	prev_builds = [b for b in prev_builds if b in available_builds]
	if not prev_builds and default_select_all_builds:
		prev_builds = list(available_builds)

	selected_builds = st.sidebar.multiselect(
		"Builds (timestamps)",
		options=available_builds,
		default=prev_builds,
		key=builds_key,
		help="Select one or more timestamped runs. If none are selected, all available builds are included.",
	)

	# If at least one family is selected but no builds are selected, treat the
	# empty selection as "all available builds for the selected families".
	# This keeps behavior consistent across viewers: choosing a family should
	# immediately scope the run grid without requiring a second click.
	if (not selected_builds) and selected_families:
		selected_builds = list(available_builds)
	elif (not selected_builds) and empty_builds_means_all:
		selected_builds = list(available_builds)

	# Optional callback for viewers that want to react to run selection updates.
	# We only call it when the effective selected_builds list changes.
	if on_runs_updated is not None:
		last_key = f"{key_prefix}_last_selected_builds"
		prev = st.session_state.get(last_key)
		# Normalize to list[str]
		prev_list: List[str] = prev if isinstance(prev, list) else []
		prev_list = [str(x) for x in prev_list]
		cur_list = [str(x) for x in selected_builds]
		if cur_list != prev_list:
			st.session_state[last_key] = list(cur_list)
			try:
				on_runs_updated(list(cur_list))
			except Exception:
				# Defensive: selection callbacks should not break the UI.
				pass

	return selected_builds, selected_families, available_builds

