import importlib.metadata
import subprocess
import sys
import re
from pathlib import Path

# 1. Update this to the path of your OLD, broken site-packages folder
OLD_SITE_PACKAGES = Path(r"C:\Projects\StockGuidance\.venv\Lib\site-packages")


def get_old_packages(path):
	"""Extracts package names from the old site-packages metadata."""
	if not path.exists():
		print(f"Error: The path '{path}' does not exist.")
		sys.exit(1)

	context = importlib.metadata.DistributionFinder.Context(path=[str(path)])
	distributions = importlib.metadata.distributions(context=context)
	skip_pkgs = {'pip', 'setuptools', 'wheel'}

	# We only return unique names, sorted alphabetically
	return sorted(list({dist.name for dist in distributions if dist.name.lower() not in skip_pkgs}))


def main():
	package_names = get_old_packages(OLD_SITE_PACKAGES)

	if not package_names:
		print("No packages found in the old site-packages directory.")
		return

	print(f"Found {len(package_names)} unique packages to process.")
	print("Fetching and upgrading to the newest current versions...\n")

	summary = {
		"Fresh Install": [],
		"Upgraded": [],
		"Already Up to Date": [],
		"Failed": []
	}

	for name in package_names:
		print(f"Processing {name}... ", end="", flush=True)

		# Run pip install with --upgrade flag. We do not use DEVNULL here
		# because we need to parse stdout to detect what pip did.
		result = subprocess.run(
			[sys.executable, "-m", "pip", "install", "--upgrade", name],
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True
		)

		if result.returncode != 0:
			print("[FAILED]")
			summary["Failed"].append((name, result.stderr.strip()))
			continue

		stdout = result.stdout

		# Parse version number from pip's successful installation text
		# e.g., "Successfully installed requests-2.32.3" or "Requirement already satisfied: requests in ..."
		version_match = re.search(rf"{re.escape(name)}-([0-9a-zA-Z.]+)", stdout, re.IGNORECASE)
		version_str = version_match.group(1) if version_match else "unknown version"

		# Determine action type based on pip stdout tracking keywords
		if "Installing collected packages:" in stdout:
			if "Attempting uninstall:" in stdout:
				print(f"[UPGRADED] ➔ version {version_str}")
				summary["Upgraded"].append(f"{name} ({version_str})")
			else:
				print(f"[FRESH INSTALL] ➔ version {version_str}")
				summary["Fresh Install"].append(f"{name} ({version_str})")
		elif "Requirement already satisfied" in stdout:
			# If already satisfied, extract the currently active version from the string
			satisfied_match = re.search(r"requirement already satisfied:.*?\((.*?)\)", stdout, re.IGNORECASE)
			current_version = satisfied_match.group(1) if satisfied_match else version_str
			print(f"[ALREADY UP TO DATE] ➔ version {current_version}")
			summary["Already Up to Date"].append(f"{name} ({current_version})")
		else:
			print(f"[SUCCESS] ➔ version {version_str}")
			summary["Fresh Install"].append(f"{name} ({version_str})")

	# --- FINAL REWRITE SUMMARY ---
	print("\n" + "=" * 50)
	print(" FINAL PROCESSING SUMMARY")
	print("=" * 50)
	for category, items in summary.items():
		if category == "Failed":
			print(f"{category}: {len(items)}")
		else:
			print(f"{category}: {len(items)}")

	if summary["Failed"]:
		print("\n❌ THE FOLLOWING PACKAGES FAILED:")
		for name, error in summary["Failed"]:
			print(f"\n- {name}")
			error_lines = error.splitlines()[:2]
			for line in error_lines:
				print(f"  Reason: {line}")


if __name__ == "__main__":
	main()
