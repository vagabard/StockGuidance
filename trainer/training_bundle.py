from dataclasses import dataclass
from typing import Optional

import torch
from torch.nn import Module
from torch.optim import Optimizer

from trainer.datasets import IndexDataset


@dataclass
class TrainingBundle:
	"""Dataset that pulls instances from the provider by 1-based index range."""
	model: Module | None
	optimizer: Optimizer | None
	scaler: torch.amp.GradScaler | None
	use_amp: bool
	amp_dtype: torch.dtype = torch.float16
	scheduler: torch.optim.lr_scheduler.ExponentialLR | None = None
	suffix: str = ""
	training_instance_provider: torch.utils.data.DataLoader = None
	validation_instance_provider: torch.utils.data.DataLoader = None
	testing_instance_provider: IndexDataset = None
	cache_path: Optional[str] = None

