"""Talk to the Analysis Services engine Power BI Desktop runs in the background (localhost only).

Through scripts/pbi-dax.ps1 (ADOMD.NET from the Power BI Desktop install): list the open reports, run DAX
queries and DMV queries ($SYSTEM.TMSCHEMA_*), or TMSL commands. Nothing leaves the machine.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pandas as pd

from .dataset import ROOT

SCRIPT = ROOT / "scripts" / "pbi-dax.ps1"


def ps(*args: str, stdin: str = "") -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
                         input=stdin.encode("utf-8"), capture_output=True, timeout=3600)
    if out.returncode:
        raise RuntimeError(out.stderr.decode("utf-8", "replace"))
    return out.stdout.decode("utf-8-sig")


def engines() -> list[dict]:
    """Open Power BI Desktop reports: [{port, exe, title}]."""
    found = json.loads(ps("-List") or "[]")
    return [found] if isinstance(found, dict) else found  # PowerShell unwraps one-element arrays


def find_port(title: str | None = None) -> int:
    """Port of the open report whose window title starts with `title` (env PBI_PORT wins)."""
    if os.environ.get("PBI_PORT"):
        return int(os.environ["PBI_PORT"])
    found = engines()
    if title:
        found = [e for e in found if (e.get("title") or "").startswith(title)]
    if not found:
        raise SystemExit(f"找不到開啟中的 Power BI Desktop 報表{f'「{title}」' if title else ''}：請先開啟報表")
    return int(found[0]["port"])


def run_dax(port: int, queries: list[str]) -> list[dict]:
    """Small results as JSON: [{rows: [[...]]} | {error}]. ASCII-only JSON: Windows PowerShell reads stdin in the
    console code page."""
    res = json.loads(ps("-Port", str(port), stdin=json.dumps(queries)))
    return res if isinstance(res, list) else [res]


def frames(port: int, queries: list[str]) -> list[pd.DataFrame]:
    """Large results as DataFrames (strings; empty cells → None), through tab-separated files."""
    tmp = Path(tempfile.mkdtemp(prefix="pbi-"))
    try:
        status = json.loads(ps("-Port", str(port), "-Out", str(tmp), stdin=json.dumps(queries)))
        status = status if isinstance(status, list) else [status]
        out = []
        for q, st in zip(queries, status):
            if "error" in st:
                raise RuntimeError(f"DAX error: {st['error'][:500]}\n{q[:500]}")
            df = pd.read_csv(st["file"], sep="\t", dtype=str, keep_default_na=False, quoting=3)
            out.append(df.replace({"": None}))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def dmv(port: int, view: str, columns: list[str]) -> pd.DataFrame:
    """A $SYSTEM.TMSCHEMA_* view as a DataFrame."""
    (df,) = frames(port, [f"SELECT {', '.join(f'[{c}]' for c in columns)} FROM $SYSTEM.{view}"])
    df.columns = columns
    return df
