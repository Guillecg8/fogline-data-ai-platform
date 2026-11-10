"""Filesystem locations used by the copilot.

Set OPS_COPILOT_HOME to run the pipeline against another project folder.
"""

import os
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("OPS_COPILOT_HOME", Path(__file__).resolve().parents[2]))
CORPUS_DIR = PROJECT_DIR / "corpus"  # bronze: raw documents, committed to git
DATA_DIR = PROJECT_DIR / "data"  # silver and gold: rebuilt by the pipeline, not committed
