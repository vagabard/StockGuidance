from __future__ import annotations

import argparse
import os
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from trainer.util.argparse_utils import general_arg_parser_understands_lists
from trainer.args_train import set_feature_flags, parse_feature_selections, parse_sweep_lists
from trainer.file_structure_family_run_handler import FileStructureFamilyRunHandler
from trainer.state.single_run import SingleRun
try:
	# Soft import: keep trainer decoupled when DB layer is unused
	from database.db_info import DbInfo, load_dbinfo
except Exception as e:  # pragma: no cover - optional dependency
	DbInfo = None  # type: ignore
	traceback.print_exception(e)
	def load_dbinfo(*args, **kwargs):  # type: ignore
		return None

# Local copies of constants previously imported from train_pytorch_network.
# Defined here to avoid a circular import between trainer.state.single_run
# and the top-level training script.

VALID_ACTIVATIONS = {
	"relu",
	"gelu",
	"tanh",
	"sigmoid",
	"sigmoid_range",
	"silu",
	"swish",
	"leaky_relu",
	"elu",
	"tanshrink",
	"softmax",
	"logsoftmax",
	"none",
}

VALID_DAY_CENTERS = {
	"one",
	"half",
	"zero",
	"gapped01",
	"binary_labels",
	"top_100_binary_labels",
	"gappednegpos",
	"binary_negpos",
	"top_100_binary_negpos",
	"normalized_labels",
	"proportional_normalized_labels",
	"normalized_zero_centered",
	"proportional_normalized_zero_centered",
	"normalized_half_centered",
	"proportional_normalized_half_centered",
	"proportional_normalized_zero_centered_gapped",
}


@dataclass
class FamilyRun:

	"""Represents a family of training runs built from CLI / argparse options.

	A ``FamilyRun`` instance encapsulates the sweep configuration and can
	materialise individual :class:`SingleRun` objects for each
	combination. It also persists a controller-style JSON state so that a
	run-family can be resumed later.
	"""

	# Core identification
	initial_run_id: str
	file_structure : FileStructureFamilyRunHandler

	# Parsed argparse namespace used as the template for child runs
	args: Any

	# Sweep configuration
	day_change_centered_list: List[str]
	prior_days_list: List[int]
	mlp_layers_list: List[int]
	lr_list: List[float]
	activation_list: List[str]
	final_activation_list: List[str]
	optimizer_list: List[str]
	include_one_feature_list: Optional[Sequence[str]]
	base_optional_features: Set[str]
	base_args_list: List[str] | None = None

	# Controller / persistence fields
	single_runs_for_all_combinations: List[SingleRun] | None = None
	current_index: int = 0
	completed: bool = False
	limit : int = 0
	current_day_change :bool = False
	volume_ratio : bool = False
	vwap_ratio : bool = False
	transaction_ratio : bool = False
	raw_train_start_with_special_characters : Optional[str] = None # keep this and the next as str, so we can write a relative number, like +100, or -100, to indicate distance from an end, as well as absolute distance
	raw_train_end_with_special_characters : Optional[str] = None
	random_training_duration : Optional[int] = None
	split_mode : Optional[str] = None
	random_seed_splits : Optional[int] = None
	train_percent : Optional[float] = None
	val_percent : Optional[float] = None
	test_percent : Optional[float] = None
	simulate_top_k : Optional[List[int]] = None
	simulate_prior_only: Optional[bool] = None
	device : Optional[str] = None
	precision : Optional[str] = None
	total_epochs_to_train : Optional[int] = None
	batch_of_instances_to_train_on_at_once : Optional[int] = None
	shuffle_instances_in_training_set : Optional[bool] = None
	number_of_GPU_workers : Optional[int] = None
	pin_GPU_memory : Optional[bool] = None
	run_repeats : Optional[int] = None

	neural_network_type : Optional[str] = None
	hidden_layer_size : Optional[List[int]] = None
	drop_last : Optional[bool] = False
	persistent_workers : Optional[bool] = None
	layer_types : Optional[List[str]] = None


	use_ranking_error_loss : bool = False
	rank_temperature : float = 0.01
	enable_rank_error_loss_in_training : bool = False
	rank_error_lower_bound : int = 1
	rank_error_upper_bound : int = 10

	dont_use_dataloader : bool = False

	log_interval : int = 50
	trading_interval : int = 0
	simulate_after_training : bool = False

	ticker_ids : List[int] = None

	# Database connection info (optional; when provided via CLI or restored from dict)
	dbinfo: Optional["DbInfo"] = None
	# The source filename used to load dbinfo (if any); kept for provenance
	dbinfo_file: Optional[str] = "dbinfo.id"


	@classmethod
	def from_args(cls, args: argparse.Namespace) -> "FamilyRun":
		# run_group_dir: str
		# metrics_file: str
		# base_run_dir: str
		# run_family_prefix:

		resume_dir = getattr(args, "resume_directory", None)
		if resume_dir:
			resume_dir = os.path.abspath(str(resume_dir))
			if not os.path.isdir(resume_dir):
				raise RuntimeError(
					f"--resume-directory does not exist or is not a directory: {resume_dir}"
				)
			return FamilyRun.load(resume_dir)
		else:
			# Construct the run/file layout helper from CLI args.
			file_structure = FileStructureFamilyRunHandler.from_args(args)
			# When invoked from inside an existing run-family directory we do not
			# create a new FamilyRun controller – the outer process is responsible
			# for that.
			base_optional_features, include_one_feature_list = parse_feature_selections(args)
			set_feature_flags(args, base_optional_features)
			(
				prior_days_list,
				mlp_layers_list,
				lr_list,
				activation_list,
				final_activation_list,
				day_change_centered_list,
				optimizer_list,
			) = parse_sweep_lists(
				args,
				valid_activations=VALID_ACTIVATIONS,
				valid_day_centers=VALID_DAY_CENTERS,
			)


			ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
			# If the caller supplied --initial-run (typically a higher-level harness
			# or batch script), we reuse that value so that all related runs can be
			# grouped together. Otherwise we default to this invocation's timestamp.
			initial_run_id = str(getattr(args, "initial_run", "") or ts)
			file_structure.set_from_initial_id(initial_run_id)

			limit = getattr(args, "limit", 0)

			raw_train_start_date_id = getattr(args, "raw_train_start_with_special_characters", None)
			raw_train_end_date_id = getattr(args, "raw_train_end_with_special_characters", None)
			random_training_duration = getattr(args, "random_training_duration", None)

			split_mode = getattr(args, "split_mode", None)
			random_seed_splits = getattr(args, "seed", None)
			train_percent = getattr(args, "train_percent", None)
			val_percent = getattr(args, "val_percent", None)
			imadict = vars(args)
			# print("args 2 ",args)
			# allKeys = vars(args).keys()
			# for (key) in allKeys:
			# 	print(key, "    equal ", (key in ["simulate_top_k"]))
			# 	if (key in ["simulate_top_k"]):
			# 		print("printing on args" , imadict.get(key) )
			simulate_top_k = [int(one_k) for one_k in general_arg_parser_understands_lists(imadict.get("simulate_top_k"), "simulate_top_k")]

			device = getattr(args, "device", None)
			precision = getattr(args, "precision", None)
			total_epochs_to_train = getattr(args, "epochs", None)
			batch_of_instances_to_train_on_at_once = getattr(args, "batch_size", None)
			shuffle_instances_in_training_set = getattr(args, "shuffle", None)
			number_of_GPU_workers = getattr(args, "num_workers", None)
			pin_GPU_memory = getattr(args, "pin_memory", None)
			run_repeats = getattr(args, "run_repeats", None)

			neural_network_type = getattr(args, "arch", None)
			hidden_layers_raw = getattr(args, "hidden_layers", "")
			hidden_layer_size = [int(x) for x in general_arg_parser_understands_lists(str(hidden_layers_raw), "hidden_layers")]
			drop_last = getattr(args, "drop_last", False)
			persistent_workers = getattr(args, "persistent_workers", None)
			layer_types_raw = getattr(args, "layer_types", "linear")
			layer_types = [x.strip() for x in general_arg_parser_understands_lists(str(layer_types_raw), "layer_types")]

			dont_use_dataloader = getattr(args, "dont_use_dataloader", False)

			base_args_source = getattr(args, "base_args_list", None)
			if base_args_source is None:
				base_args_source = getattr(args, "base_args", None)
			if base_args_source is None:
				base_args_list: List[str] = []
			elif isinstance(base_args_source, (list, tuple, set)):
				base_args_list = [str(v) for v in base_args_source]
			else:
				base_args_list = [str(base_args_source)]



			# Try to populate database info if CLI provided a dbinfo file
			dbinfo_obj = None
			dbinfo_file: Optional[str] =  getattr(args, "dbinfo_file", "dbinfo.id")
			try:
				loaded = load_dbinfo(dbinfo_file)
				if loaded is not None:
					# type: ignore[attr-defined]
					dbinfo_obj = loaded
			except Exception as e:
				traceback.print_exception(e)
				# Leave as None if loading fails; callers that need DB must pass it explicitly
				pass

			new_family_run = cls(
				initial_run_id=str(initial_run_id),
				args=args,
				day_change_centered_list=list(day_change_centered_list),
				prior_days_list=list(prior_days_list),
				mlp_layers_list=list(mlp_layers_list),
				lr_list=list(lr_list),
				activation_list=list(activation_list),
				final_activation_list=list(final_activation_list),
				optimizer_list=list(optimizer_list),
				include_one_feature_list=list(include_one_feature_list) if include_one_feature_list else None,
				base_optional_features=set(base_optional_features),
				base_args_list=list(base_args_list),
				single_runs_for_all_combinations=[],
				current_index=0,
				completed=False,
				limit=limit,
				volume_ratio=args.volume_ratio,
				vwap_ratio=args.vwap_ratio,
				transaction_ratio=args.transaction_ratio,
				raw_train_start_with_special_characters=raw_train_start_date_id,
				raw_train_end_with_special_characters=raw_train_end_date_id,
				random_training_duration=random_training_duration,
				split_mode=split_mode,
				random_seed_splits=random_seed_splits,
				train_percent=train_percent,
				val_percent=val_percent,
				simulate_top_k=simulate_top_k,
				simulate_prior_only=args.simulate_prior_only,
				precision=precision,
				dbinfo=dbinfo_obj,
				dbinfo_file=dbinfo_file,
				device=device,
				total_epochs_to_train=total_epochs_to_train,
				batch_of_instances_to_train_on_at_once=batch_of_instances_to_train_on_at_once,
				dont_use_dataloader=dont_use_dataloader,
				shuffle_instances_in_training_set=shuffle_instances_in_training_set,
				number_of_GPU_workers=number_of_GPU_workers,
				pin_GPU_memory=pin_GPU_memory,
				run_repeats=run_repeats,
				neural_network_type=neural_network_type,
				hidden_layer_size=hidden_layer_size,
				drop_last=drop_last,
				persistent_workers=persistent_workers,
				layer_types=layer_types,
				log_interval=args.log_interval,
				use_ranking_error_loss=args.use_ranking_error_loss,
				rank_temperature=args.rank_temp,
				enable_rank_error_loss_in_training=args.enable_rank_err,
				rank_error_lower_bound=args.rank_lower,
				rank_error_upper_bound=args.rank_upper,
				trading_interval=args.trading_interval,
				simulate_after_training=args.simulate_after,
				file_structure=file_structure,
				ticker_ids=getattr(args, "ticker_ids", None)
			)
			new_family_run.file_structure = file_structure
			return new_family_run

	def create_options_for_individual_runs(self) -> None:
		"""Create single_runs_for_all_combinations and corresponding subprocess command lines.

		Returns ``(single_runs_for_all_combinations, family_commands)`` where ``single_runs_for_all_combinations`` is the
		list of JSON-serialisable entries and ``family_commands`` the argv lists
		to be used by the controller loop.
		"""

		args = self.args

		one_feature_values: Sequence[Optional[str]]
		if self.include_one_feature_list:
			one_feature_values = list(self.include_one_feature_list)
		else:
			one_feature_values = [None]

		single_runs_for_all_combinations: List[SingleRun] = []
		combo_id = 0
		base_timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())

		for dc in self.day_change_centered_list:
			for pd in self.prior_days_list:
				for ml in self.mlp_layers_list:
					for lr in self.lr_list:
						for final_act in self.final_activation_list:
							for act in self.activation_list:
								for opt in self.optimizer_list:
									for one_feat in one_feature_values:
										selected_features = set(self.base_optional_features)
										if one_feat is not None:
											selected_features.add(str(one_feat))
										set_feature_flags(args, selected_features)
										current_timestamp_id = f"{base_timestamp}_{combo_id:02d}"
										lrap = float(getattr(self.args, "learning_rate_adjustment_parameter", 0.998))
										run_state = SingleRun(
											timestamp_id=current_timestamp_id,
											family_run=self,
											day_change_centered=str(dc),
											prior_days=int(pd),
											mlp_layers=int(ml),
											lr=float(lr),
											learning_rate_adjustment_parameter=lrap,
											activation=str(act),
											final_activation=str(final_act),
											optimizer=str(opt),
											include_features=selected_features or None,
											train_start_date_id=None,
											train_end_date_id=None,
										)
										single_runs_for_all_combinations.append(run_state)
										combo_id += 1

		self.single_runs_for_all_combinations = single_runs_for_all_combinations

	@property
	def family_dir(self) -> str | None:
		"""Expose the family directory resolved by the underlying file structure."""
		return getattr(self.file_structure, "family_dir", None)

	@property
	def state_path(self) -> str | None:
		"""Expose the path to the persisted family state file."""
		return getattr(self.file_structure, "family_state_path", None)

	def to_dict(self) -> Dict[str, Any]:
		"""Return a JSON-serialisable representation of the family state.

		This implementation avoids ``dataclasses.asdict`` to prevent infinite
		recursion via the ``SingleRun.single_run`` back-reference. Instead we
		shallow-copy ``__dict__`` and manually normalise fields that need special
		handling.
		"""

		from trainer.state.single_run import SingleRun  # local import to avoid cycles
		from trainer.file_structure_family_run_handler import FileStructureFamilyRunHandler

		data: Dict[str, Any] = dict(self.__dict__)
		# ``args`` is not JSON-serialisable; callers that need it should persist
		# ``vars(args)`` separately. The controller only needs combinations etc.
		data.pop("args", None)
		# ``file_structure`` is a helper object; persist only its simple
		# path-like attributes via ``FileStructureFamilyRunHandler.to_dict``.
		if isinstance(self.file_structure, FileStructureFamilyRunHandler):
			data["file_structure"] = self.file_structure.to_dict()
		# Normalise sweep configuration containers so that JSON contains only
		# built-in types (lists, numbers, strings).
		data["day_change_centered_list"] = [str(v) for v in self.day_change_centered_list]
		data["prior_days_list"] = [int(v) for v in self.prior_days_list]
		data["mlp_layers_list"] = [int(v) for v in self.mlp_layers_list]
		data["lr_list"] = [float(v) for v in self.lr_list]
		data["activation_list"] = [str(v) for v in self.activation_list]
		data["final_activation_list"] = [str(v) for v in self.final_activation_list]
		data["optimizer_list"] = [str(v) for v in self.optimizer_list]
		# ``include_one_feature_list`` may be a sequence or ``None``; normalise to
		# a plain list for JSON.
		if self.include_one_feature_list is not None:
			data["include_one_feature_list"] = [str(v) for v in self.include_one_feature_list]
		# ``base_optional_features`` is a ``set[str]`` and must be turned into a
		# list for JSON serialisation.
		data["base_optional_features"] = sorted(str(v) for v in self.base_optional_features)
		data["base_args"] = list(self.base_args_list or [])
		data["hidden_layer_size"] = [int(v) for v in self.hidden_layer_size] if self.hidden_layer_size is not None else None
		data["layer_types"] = [str(v) for v in self.layer_types] if self.layer_types is not None else None

		def _normalize_to_list(val: Any) -> Any:
			if hasattr(val, "tolist"):  # Handle numpy arrays
				return val.tolist()
			if isinstance(val, (list, tuple, set)):
				return [_normalize_to_list(v) for v in val]
			return val

		data["ticker_ids"] = _normalize_to_list(self.ticker_ids)
		data["simulate_top_k"] = _normalize_to_list(self.simulate_top_k)

		# Normalise the combinations list to plain dictionaries so that
		# back-references from SingleRun -> FamilyRun are not embedded.
		serialised_single_runs: List[Dict[str, Any]] = []
		for sr in self.single_runs_for_all_combinations or []:
			if isinstance(sr, SingleRun):
				serialised_single_runs.append(sr.to_dict())
			else:
				serialised_single_runs.append(sr)  # type: ignore[arg-type]
		data["single_runs_for_all_combinations"] = serialised_single_runs

		# Serialize DB info if present
		if getattr(self, "dbinfo", None) is not None:
			try:
				dbi = self.dbinfo  # type: ignore[attr-defined]
				data["dbinfo"] = {
					"host": getattr(dbi, "host", None),
					"database": getattr(dbi, "database", None),
					"user": getattr(dbi, "user", None),
					"password": getattr(dbi, "password", None),
					"port": getattr(dbi, "port", None),
					"driver": getattr(dbi, "driver", None),
					"schema": getattr(dbi, "schema", None),
				}
			except Exception:
				# Ignore serialization errors and omit field
				pass
		# Include dbinfo_file when available
		if getattr(self, "dbinfo_file", None):
			data["dbinfo_file"] = str(self.dbinfo_file)
		return data

	@classmethod
	def from_dict(cls, data: Dict[str, Any]) -> "FamilyRun":
		"""Reconstruct a :class:`FamilyRun` from :meth:`to_dict` output.

		This helper mirrors :meth:`to_dict` and is intended for cases where a
		full ``FamilyRun`` (including its ``FileStructureFamilyRunHandler``) was
		serialised, such as the payload embedded in ``SingleRun.save``.
		"""

		from trainer.file_structure_family_run_handler import FileStructureFamilyRunHandler

		# Rehydrate ``file_structure`` from its dictionary representation if
		# present; otherwise assume the caller will supply one explicitly.
		fs_raw = data.get("file_structure")
		if isinstance(fs_raw, dict):
			file_structure = FileStructureFamilyRunHandler.from_dict(fs_raw)
		else:
			# Fall back to any already-constructed helper or a bare instance.
			file_structure = fs_raw if isinstance(fs_raw, FileStructureFamilyRunHandler) else FileStructureFamilyRunHandler()

		# Normalise container fields; accept both lists and sets in case older
		# payloads stored them differently.
		def _as_str_list(value: Any) -> List[str]:
			if value is None:
				return []
			if isinstance(value, (list, tuple, set)):
				return [str(v) for v in value]
			return [str(value)]

		def _as_int_list(value: Any) -> List[int]:
			if value is None:
				return []
			if isinstance(value, (list, tuple, set)):
				return [int(v) for v in value]
			return [int(value)]

		def _as_float_list(value: Any) -> List[float]:
			if value is None:
				return []
			if isinstance(value, (list, tuple, set)):
				return [float(v) for v in value]
			return [float(value)]

		initial_run_id = str(data.get("initial_run_id", ""))
		day_change_centered_list = _as_str_list(data.get("day_change_centered_list"))
		prior_days_list = _as_int_list(data.get("prior_days_list"))
		mlp_layers_list = _as_int_list(data.get("mlp_layers_list"))
		lr_list = _as_float_list(data.get("lr_list"))
		activation_list = _as_str_list(data.get("activation_list"))
		final_activation_list = _as_str_list(data.get("final_activation_list"))
		optimizer_list = _as_str_list(data.get("optimizer_list"))
		if not optimizer_list:
			optimizer_list = ["adam"]
		include_one_feature_raw = data.get("include_one_feature_list")
		include_one_feature_list = (
			_as_str_list(include_one_feature_raw) if include_one_feature_raw is not None else None
		)
		base_optional_raw = data.get("base_optional_features") or []
		if isinstance(base_optional_raw, (list, tuple, set)):
			base_optional_features = set(str(v) for v in base_optional_raw)
		else:
			base_optional_features = {str(base_optional_raw)} if base_optional_raw else set()
		base_args_list = _as_str_list(data.get("base_args"))

		# ``single_runs_for_all_combinations`` is persisted as a list of plain
		# dictionaries. The controller only needs these when resuming; they are
		# passed through as-is here.
		single_runs_for_all_combinations = list(data.get("single_runs_for_all_combinations", []) or [])
		current_index = int(data.get("current_index", 0) or 0)
		completed = bool(data.get("completed", False))

		# Additional controller/training fields (all optional; defaulted when absent)
		def _get_bool(name: str, default: bool = False) -> bool:
			return bool(data.get(name, default))

		def _get_int(name: str, default: int | None = None) -> int | None:
			v = data.get(name, default)
			if v is None or v == "":
				return default
			try:
				return int(v)
			except Exception:
				return default

		def _get_float(name: str, default: float | None = None) -> float | None:
			v = data.get(name, default)
			if v is None or v == "":
				return default
			try:
				return float(v)
			except Exception:
				return default

		def _get_str(name: str, default: str | None = None) -> str | None:
			v = data.get(name, default)
			if v is None:
				return default
			return str(v)

		limit = _get_int("limit")
		volume_ratio = _get_bool("volume_ratio")
		vwap_ratio = _get_bool("vwap_ratio")
		transaction_ratio = _get_bool("transaction_ratio") or _get_bool("tranaction_ratio")
		raw_train_start_date_id = _get_str("raw_train_start_with_special_characters")
		raw_train_end_date_id = _get_str("raw_train_end_with_special_characters")
		random_training_duration = _get_int("random_training_duration")
		split_mode = _get_str("split_mode")
		random_seed_splits = _get_int("random_seed_splits")
		train_percent = _get_float("train_percent")
		val_percent = _get_float("val_percent")
		test_percent = _get_float("test_percent")
		if train_percent is None:
			train_percent = 0.0
		if val_percent is None:
			val_percent = 0.0
		if test_percent is None:
			test_percent = 1.0 - train_percent - val_percent

		# Validation: ensure percentages are sane if they were explicitly provided.
		# If we're loading an old state where they don't quite sum to 1, we relax
		# the check to avoid breaking the viewer.
		total_p = train_percent + val_percent + test_percent
		assert 0.9 <= total_p <= 1.1, f"train_percent ({train_percent}) + val_percent ({val_percent}) + test_percent ({test_percent}) must sum to approximately 1 (got {total_p})"

		simulate_top_k =  _as_int_list(data.get("simulate_top_k"))
		simulate_prior_only = _get_bool("simulate_prior_only")
		precision = _get_str("precision")
		device = data.get("device")  # may be string like 'cpu'/'cuda', leave as-is
		total_epochs_to_train = _get_int("total_epochs_to_train") or 0
		batch_of_instances_to_train_on_at_once = _get_int("batch_of_instances_to_train_on_at_once") or 1
		dont_use_dataloader = _get_bool("dont_use_dataloader")
		shuffle_instances_in_training_set = _get_bool("shuffle_instances_in_training_set")
		number_of_GPU_workers = _get_int("number_of_GPU_workers") or 0
		pin_GPU_memory = _get_bool("pin_GPU_memory")
		run_repeats = _get_int("run_repeats") or 1
		neural_network_type = _get_str("neural_network_type") or "mlp"
		hidden_layer_size = _as_int_list(data.get("hidden_layer_size"))
		layer_types = _as_str_list(data.get("layer_types"))
		if not hidden_layer_size: # Fallback for old states that might have a single int
			old_val = _get_int("hidden_layer_size")
			if old_val is not None:
				hidden_layer_size = [old_val]
			else:
				hidden_layer_size = []

		drop_last = _get_bool("drop_last")
		persistent_workers = _get_bool("persistent_workers")
		log_interval = _get_int("log_interval") or 0
		use_ranking_error_loss = _get_bool("use_ranking_error_loss")
		rank_temperature = _get_float("rank_temperature") or 1.0
		enable_rank_error_loss_in_training = _get_bool("enable_rank_error_loss_in_training")
		rank_error_lower_bound = _get_int("rank_error_lower_bound")
		rank_error_upper_bound = _get_int("rank_error_upper_bound")
		trading_interval = _get_int("trading_interval")
		simulate_after_training = _get_bool("simulate_after_training") or _get_bool("simulate_after")
		current_day_change = _get_bool("current_day_change") or _get_bool("change_in_price_today")
		ticker_ids = _as_int_list(data.get("ticker_ids")) if data.get("ticker_ids") is not None else None

		# Rehydrate optional DB info if present
		dbinfo_obj = None
		dbinfo_file: Optional[str] = None
		try:
			dbinfo_raw = data.get("dbinfo")
			if isinstance(dbinfo_raw, dict) and DbInfo is not None:
				dbinfo_obj = DbInfo(
					host=str(dbinfo_raw.get("host", "localhost")),
					database=str(dbinfo_raw.get("database", "stockforecasting")),
					user=dbinfo_raw.get("user"),
					password=dbinfo_raw.get("password"),
					port=int(dbinfo_raw.get("port", 3306)),
					driver=str(dbinfo_raw.get("driver", "auto")),
					schema=dbinfo_raw.get("schema"),
				)
		except Exception as e:
			traceback.print_exception(e)
			# Leave as None on any issue
			pass

		# Also load optional dbinfo_file if present
		dbinfo_file_val = data.get("dbinfo_file")
		if isinstance(dbinfo_file_val, str) and dbinfo_file_val:
			try:
				dbinfo_file = str(dbinfo_file_val)
			except Exception as e:
				traceback.print_exception(e)
				dbinfo_file = None

		return cls(
			initial_run_id=initial_run_id,
			file_structure=file_structure,
			args=None,
			day_change_centered_list=day_change_centered_list,
			prior_days_list=prior_days_list,
			mlp_layers_list=mlp_layers_list,
			lr_list=lr_list,
			activation_list=activation_list,
			final_activation_list=final_activation_list,
			optimizer_list=optimizer_list,
			include_one_feature_list=include_one_feature_list,
			base_optional_features=base_optional_features,
			base_args_list=base_args_list,
			single_runs_for_all_combinations=single_runs_for_all_combinations,
			current_index=current_index,
			completed=completed,
			limit=limit,
			volume_ratio=volume_ratio,
			vwap_ratio=vwap_ratio,
			transaction_ratio=transaction_ratio,
			raw_train_start_with_special_characters=raw_train_start_date_id,
			raw_train_end_with_special_characters=raw_train_end_date_id,
			random_training_duration=random_training_duration,
			split_mode=split_mode,
			random_seed_splits=random_seed_splits,
			train_percent=train_percent,
			val_percent=val_percent,
			test_percent=test_percent,
			simulate_top_k=simulate_top_k,
			simulate_prior_only=simulate_prior_only,
			precision=precision,
			dbinfo=dbinfo_obj,
			dbinfo_file=dbinfo_file,
			device=device,
			total_epochs_to_train=total_epochs_to_train,
			batch_of_instances_to_train_on_at_once=batch_of_instances_to_train_on_at_once,
			dont_use_dataloader=dont_use_dataloader,
			shuffle_instances_in_training_set=shuffle_instances_in_training_set,
			number_of_GPU_workers=number_of_GPU_workers,
			pin_GPU_memory=pin_GPU_memory,
			run_repeats=run_repeats,
			neural_network_type=neural_network_type,
			hidden_layer_size=hidden_layer_size,
			drop_last=drop_last,
			persistent_workers=persistent_workers,
			layer_types=layer_types,
			log_interval=log_interval,
			use_ranking_error_loss=use_ranking_error_loss,
			rank_temperature=rank_temperature,
			enable_rank_error_loss_in_training=enable_rank_error_loss_in_training,
			rank_error_lower_bound=rank_error_lower_bound,
			rank_error_upper_bound=rank_error_upper_bound,
			trading_interval=trading_interval,
			simulate_after_training=simulate_after_training,
			current_day_change=current_day_change,
			ticker_ids=ticker_ids,
		)

	def save(self) -> Tuple[Dict[str, Any], str]:
		"""Persist the controller state to ``family_state.json``.

		Returns the dictionary that was written, for convenience.
		"""

		if not self.file_structure.family_state_path:
			raise RuntimeError("FamilyRun.family_state_path is not set")

		state = self.to_dict()
		os.makedirs(os.path.dirname(self.file_structure.family_state_path), exist_ok=True)
		with open(self.file_structure.family_state_path, "w", encoding="utf-8") as sf:
			import json

			json.dump(state, sf, indent=2, sort_keys=True)
		return state, self.file_structure.family_state_path

	@classmethod
	def load(cls, resume_dir: str) -> "FamilyRun":
		"""Load a ``FamilyRun`` from an existing ``family_state.json``.

		Note that ``args`` and sweep lists cannot be reconstructed perfectly from
		JSON alone; this loader focuses on controller fields needed for
		resumption (combinations, indices, etc.). Callers are expected to
		reconstruct or re-supply ``args`` separately if needed.
		"""
		file_structure = FileStructureFamilyRunHandler.load(resume_dir)
		import json

		with open(file_structure.family_state_path, "r", encoding="utf-8") as sf:
			state: Dict[str, Any] = json.load(sf)

		# Prefer the comprehensive mapping in from_dict so we restore all known
		# fields from JSON. ``file_structure`` is supplied based on the resume dir
		# and then explicitly re-attached to ensure we do not depend on what was
		# persisted in the JSON.
		obj = cls.from_dict(state)
		obj.file_structure = file_structure
		return obj


__all__ = ["FamilyRun"]
