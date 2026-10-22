"""Background work after an upload, step by step so the page can show progress.

A report (.pbix): read the model and the imported data from the file, draft the analysis settings, take the
snapshots, write the latest daily report. A workbook (.xlsx): its sheets / Excel tables as plain tables next to
the reports. Then the report map is rebuilt. Every step is the same command a person would run
(python -m analysis …), so what PRISM does can be reproduced by hand.
"""
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from analysis.dataset import ROOT

from . import registry


@dataclass
class Step:
    title: str
    status: str = "waiting"   # waiting / running / done / failed
    detail: str = ""


@dataclass
class Job:
    id: str
    name: str
    title: str
    steps: list[Step] = field(default_factory=list)
    status: str = "running"
    error: str = ""
    started: float = field(default_factory=time.time)

    def view(self) -> dict:
        return asdict(self)


JOBS: dict[str, Job] = {}
LOCK = threading.Lock()  # one pipeline at a time: they share the CPU and the catalog
on_finished = []          # callbacks (the app rebuilds the report map)


def _run(step: Step, args: list[str], dataset: str | None = None, timeout: int = 3600) -> str:
    step.status = "running"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if dataset:
        env["PBI_DATASET"] = dataset
    p = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    lines = [x for x in (p.stdout + "\n" + p.stderr).splitlines() if x.strip()]
    if p.returncode:
        step.status = "failed"
        step.detail = "\n".join(lines[-8:])
        raise RuntimeError(lines[-1] if lines else f"exit {p.returncode}")
    step.status = "done"
    step.detail = "\n".join(lines[-6:])
    return p.stdout


def start_report(name: str, title: str, pbix: Path, report_url: str, workbooks: list[Path]) -> Job:
    registry.save_meta(name, title=title, kind="report", status="processing", report_url=report_url,
                       created_at=time.time(), file=pbix.name, workbooks=[])
    job = Job(uuid.uuid4().hex[:12], name, title, [
        Step("讀取報表模型與資料（不需要 Power BI）"), Step("依資料推出分析設定"), Step("建立每日快照"),
        Step("產生最新一天的變動報告"), *[Step(f"加入 Excel：{w.name}") for w in workbooks], Step("更新報表地圖")])
    JOBS[job.id] = job
    threading.Thread(target=_report_pipeline, args=(job, pbix, workbooks), daemon=True).start()
    return job


def start_workbooks(parent: str, workbooks: list[Path]) -> Job:
    meta = registry.load_meta(parent) or registry.EXAMPLES.get(parent, {})
    job = Job(uuid.uuid4().hex[:12], parent, meta.get("title", parent),
              [*[Step(f"加入 Excel：{w.name}") for w in workbooks], Step("更新報表地圖")])
    JOBS[job.id] = job
    threading.Thread(target=_workbook_pipeline, args=(job, workbooks), daemon=True).start()
    return job


def _report_pipeline(job: Job, pbix: Path, workbooks: list[Path]) -> None:
    with LOCK:
        try:
            s = job.steps
            out = _run(s[0], ["-m", "analysis", "import-pbix", str(pbix), "--name", job.name,
                              "--root", str(registry.WORKSPACE)])
            s[1].status = "done"
            s[1].detail = "\n".join(x for x in out.splitlines() if x.startswith(("事實表", "日期", "維度", "指標角色")))
            _run(s[2], ["-m", "analysis", "snapshot"], dataset=job.name)
            _run(s[3], ["-m", "analysis", "report"], dataset=job.name)
            for step, w in zip(s[4:-1], workbooks):
                _add_workbook(step, job.name, w)
            registry.save_meta(job.name, status="ready")
            _rebuild_map(s[-1])
            job.status = "done"
        except Exception as e:  # noqa: BLE001 - shown on the page, the upload is kept for a retry
            job.status, job.error = "failed", str(e)
            registry.save_meta(job.name, status="failed", error=str(e))


def _workbook_pipeline(job: Job, workbooks: list[Path]) -> None:
    with LOCK:
        try:
            for step, w in zip(job.steps[:-1], workbooks):
                _add_workbook(step, job.name, w)
            _rebuild_map(job.steps[-1])
            job.status = "done"
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "failed", str(e)


def _add_workbook(step: Step, parent: str, path: Path) -> None:
    name = registry.new_name(f"{parent}-{path.stem}")
    _run(step, ["-m", "analysis", "import-excel", str(path), "--name", name, "--title", path.stem,
                "--root", str(registry.WORKSPACE)])
    registry.save_meta(name, title=path.name, kind="workbook", status="ready", parent=parent,
                       created_at=time.time(), file=path.name)
    if registry.meta_path(parent).exists():
        meta = registry.load_meta(parent)
        registry.save_meta(parent, workbooks=[*meta.get("workbooks", []), name])


def _rebuild_map(step: Step) -> None:
    step.status = "running"
    for callback in on_finished:
        callback()
    step.status = "done"
