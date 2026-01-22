from dataclasses import dataclass

import torch
from torch.nn import Module
from torch.optim import Optimizer

from trainer.datasets import IndexDataset


@dataclass
class TrainingBundle:
	"""Dataset that pulls instances from the provider by 1-based index range."""
	model: Module
	optimizer: Optimizer
	scaler: torch.amp.GradScaler
	use_amp: bool
	amp_dtype: torch.dtype = torch.float16
	scheduler: torch.optim.lr_scheduler.ExponentialLR = None
	suffix: str = ""
	training_instance_provider: torch.utils.data.DataLoader = None
	validation_instance_provider: torch.utils.data.DataLoader = None
	testing_instance_provider: IndexDataset = None

