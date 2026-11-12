"""Filesystem locations used by the lakehouse.

Set LAKEHOUSE_HOME to run the pipeline against another project folder.
"""

import os
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("LAKEHOUSE_HOME", Path(__file__).resolve().parents[2]))
REPO_ROOT = Path(__file__).resolve().parents[4]
GOLD_CONTRACT = REPO_ROOT / "contracts" / "gold_kpis.yaml"  # what consumers expect from us
CONTRACTS_DIR = PROJECT_DIR / "contracts"
REFERENCE_DIR = PROJECT_DIR / "reference"
DATA_DIR = PROJECT_DIR / "data"  # landing, bronze, silver, gold: rebuilt, not committed
