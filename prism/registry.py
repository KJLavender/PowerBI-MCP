"""The reports and workbooks PRISM knows: the examples in datasets/ and what people uploaded to workspace/.

Each uploaded item has workspace/<name>/prism.json: title, kind (report / workbook), status, the optional
address of the published report (shown next to the chat), the workbooks attached to a report.
"""
import json
import os
import re
import time
from pathlib import Path

from analysis.dataset import ROOT

WORKSPACE = ROOT / "workspace"
EXAMPLES = {  # the datasets that come with the repository
    "retail": {"title": "零售銷售（範例）", "kind": "report",
               "report_url": "http://localhost:8081/Reports/powerbi/RetailSales"},
    "superstore": {"title": "Global Superstore（Excel 範例）", "kind": "report"},
    "retail_pbix": {"title": "零售銷售 .pbix 匯入（範例）", "kind": "report"},
    "store_targets": {"title": "財務部門市目標（跨報表範例）", "kind": "report"},
}


def meta_path(name: str) -> Path:
    return WORKSPACE / name / "prism.json"


def load_meta(name: str) -> dict:
    p = meta_path(name)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_meta(name: str, **changes) -> dict:
    meta = {**load_meta(name), **changes, "updated_at": time.time()}
    meta_path(name).parent.mkdir(parents=True, exist_ok=True)
    meta_path(name).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def home(name: str) -> Path | None:
    if name == "retail":
        return ROOT
    for base in (ROOT / "datasets", WORKSPACE):
        if (base / name).is_dir():
            return base / name
    return None


def data_dir(name: str) -> Path:
    return ROOT / "data" if name == "retail" else ROOT / "data" / name


def chat_ready(name: str) -> bool:
    """A chat needs the analysis settings and the snapshots."""
    h = home(name)
    if h is None:
        return False
    config = ROOT / "analysis" / "config.yaml" if name == "retail" else h / "analysis.yaml"
    return config.exists() and (data_dir(name) / "analysis.duckdb").exists()


def example_shown(name: str) -> bool:
    """The examples read the Oracle simulator, set up by the repository's .env (not part of a download):
    elsewhere PRISM starts empty, with what people upload. PRISM_EXAMPLES=1 / 0 forces them on / off."""
    forced = os.environ.get("PRISM_EXAMPLES", "")
    return home(name) is not None and (forced == "1" or forced != "0" and (ROOT / ".env").exists())


def names() -> list[str]:
    """What the portal lists and the report map compares."""
    uploaded = [d.name for d in WORKSPACE.iterdir() if load_meta(d.name)] if WORKSPACE.is_dir() else []
    return [n for n in EXAMPLES if example_shown(n)] + uploaded


def entries() -> list[dict]:
    """Every item, uploaded first (newest first), then the examples."""
    out = []
    for d in sorted(WORKSPACE.iterdir(), key=lambda p: -p.stat().st_mtime) if WORKSPACE.is_dir() else []:
        meta = load_meta(d.name)
        if meta:
            out.append({"name": d.name, "origin": "uploaded", "chat": chat_ready(d.name), **meta})
    for name, meta in EXAMPLES.items():
        if example_shown(name):
            out.append({"name": name, "origin": "example", "status": "ready", "chat": chat_ready(name), **meta})
    return out


def new_name(title: str) -> str:
    """A folder name for an upload: ASCII from the title, unique among datasets and uploads."""
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "report"
    name, i = base, 2
    while home(name) is not None or name in EXAMPLES:
        name, i = f"{base}-{i}", i + 1
    return name
