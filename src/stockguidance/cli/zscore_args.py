import argparse


def parse_windows(value: str, option: str = "--zscore-windows") -> tuple[int, ...]:
	if not isinstance(value, str) or not value.strip():
		raise argparse.ArgumentTypeError(f"{option} requires comma-separated integers >= 2")
	try:
		windows = tuple(int(item.strip()) for item in value.split(","))
	except ValueError:
		raise argparse.ArgumentTypeError(f"{option} requires comma-separated integers >= 2") from None
	if not windows or any(type(window) is not int or window < 2 for window in windows) or len(set(windows)) != len(windows):
		raise argparse.ArgumentTypeError(f"{option} requires distinct integers >= 2")
	return windows
