from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.reporting.artifacts import artifact_payload


def main() -> None:
	import streamlit as st

	st.set_page_config(page_title="StockGuidance artifact", layout="wide")
	st.title("Inference artifact")
	path = st.text_input("Explicit .sgmodel path")
	identity = st.text_input("Artifact SHA256 identity")
	if not path or not identity:
		return
	try:
		st.json(artifact_payload(ArtifactReference(Path(path), identity)))
	except Exception:
		st.error("The artifact is unavailable, incompatible, or has a different identity.")


if __name__ == "__main__":
	main()
