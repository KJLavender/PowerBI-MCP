"""Which report the analysis runs on.

    PBI_DATASET=retail      (default) powerbi/ + analysis/config.yaml, data from Oracle
    PBI_DATASET=<name>      datasets/<name>/powerbi + datasets/<name>/analysis.yaml

The data source is whatever the Power BI model's partitions read (Oracle tables, Excel sheets);
snapshots and generated reports are kept per dataset.

PBI_VARIANT=<name> reads another workbook listed under `variants:` in the dataset config instead of the
one in the model (e.g. the original workbook without injected events), with its own snapshots.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = os.environ.get("PBI_DATASET", "retail")
VARIANT = os.environ.get("PBI_VARIANT") or None

if NAME == "retail":
    HOME = ROOT
    PBIP_DIR = ROOT / "powerbi"
    CONFIG = ROOT / "analysis" / "config.yaml"
    DATA_DIR = ROOT / "data"
    REPORT_DIR = ROOT / "reports"
    QUESTIONS = ROOT / "agent" / "questions.yaml"
else:
    HOME = ROOT / "datasets" / NAME
    if not HOME.is_dir():
        raise SystemExit(f"PBI_DATASET={NAME}: folder {HOME} does not exist")
    PBIP_DIR = HOME / "powerbi"
    CONFIG = HOME / "analysis.yaml"
    DATA_DIR = ROOT / "data" / (f"{NAME}-{VARIANT}" if VARIANT else NAME)
    REPORT_DIR = HOME / "reports" / VARIANT if VARIANT else HOME / "reports"
    QUESTIONS = HOME / "questions.yaml"
