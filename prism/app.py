"""The PRISM portal: upload reports and workbooks, open a report's chat, see how everything relates."""
import asyncio
import re
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from analysis.dataset import ROOT

from . import FULL_NAME, NAME, TAGLINE, TAGLINE_EN, VERSION, jobs, model, registry, runner

STATIC = Path(__file__).with_name("static")
app = FastAPI(title=f"{NAME} — {FULL_NAME}")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
BIND = {"host": "127.0.0.1"}  # set by __main__: chat servers listen where the portal listens

MAX_PBIX = 1024 ** 3          # 1 GB
EXCEL = (".xlsx", ".xlsm")


def _known(name: str) -> str:
    """A report name from the address: only the folder names PRISM makes (never a path)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) or registry.home(name) is None:
        raise HTTPException(404, "沒有這份報表")
    return name


# ------------------------------------------------------------------------------------------- pages
@app.get("/")
async def home():
    return FileResponse(STATIC / "home.html")


@app.get("/map")
async def map_page():
    return FileResponse(STATIC / "map.html")


@app.get("/how")
async def how_page():
    return FileResponse(STATIC / "how.html")


@app.get("/api/brand")
async def brand():
    return {"name": NAME, "full_name": FULL_NAME, "tagline": TAGLINE, "tagline_en": TAGLINE_EN, "version": VERSION}


@app.get("/api/model")
async def model_status():
    """The local model behind the chats (checked again until it is there)."""
    if model.STATUS["state"] in ("no_ollama", "pull_failed"):
        await asyncio.to_thread(model.check)
    return model.STATUS


# ------------------------------------------------------------------------------------------- reports
@app.get("/api/reports")
async def reports():
    return registry.entries()


@app.post("/api/reports")
async def upload(title: str = Form(""), report_url: str = Form(""), pbix: UploadFile = File(...),
                 excel: list[UploadFile] | None = File(None)):
    if not (pbix.filename or "").lower().endswith(".pbix"):
        return JSONResponse({"error": "請上傳 Power BI 報表檔（.pbix）"}, status_code=400)
    bad = [f.filename for f in excel or [] if not (f.filename or "").lower().endswith(EXCEL)]
    if bad:
        return JSONResponse({"error": f"Excel 只接受 .xlsx / .xlsm：{'、'.join(bad)}"}, status_code=400)
    title = title.strip() or Path(pbix.filename).stem
    name = registry.new_name(title)
    files = registry.WORKSPACE / name / "files"
    files.mkdir(parents=True, exist_ok=True)
    try:
        saved = await _save(pbix, files, MAX_PBIX)
        books = [await _save(f, files, MAX_PBIX) for f in excel or [] if f.filename]
    except ValueError as e:
        shutil.rmtree(registry.WORKSPACE / name, ignore_errors=True)
        return JSONResponse({"error": str(e)}, status_code=413)
    job = jobs.start_report(name, title, saved, report_url.strip(), books)
    return job.view()


@app.post("/api/reports/{name}/excel")
async def add_excel(name: str, excel: list[UploadFile] = File(...)):
    _known(name)
    files = registry.WORKSPACE / name / "files"
    files.mkdir(parents=True, exist_ok=True)
    books = [await _save(f, files, MAX_PBIX) for f in excel if (f.filename or "").lower().endswith(EXCEL)]
    if not books:
        return JSONResponse({"error": "請選擇 .xlsx / .xlsm 檔"}, status_code=400)
    return jobs.start_workbooks(name, books).view()


async def _save(f: UploadFile, folder: Path, limit: int) -> Path:
    target = folder / Path(f.filename).name  # no paths from the browser
    size = 0
    with open(target, "wb") as out:
        while chunk := await f.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                target.unlink(missing_ok=True)
                raise ValueError(f"{f.filename} 太大（上限 {limit // 1024 ** 2} MB）")
            out.write(chunk)
    return target


@app.get("/api/jobs/{job_id}")
async def job(job_id: str):
    j = jobs.JOBS.get(job_id)
    return j.view() if j else JSONResponse({"error": "沒有這個工作"}, status_code=404)


@app.post("/api/reports/{name}/open")
async def open_report(name: str, request: Request):
    """Start (or find) the report's chat server and give its address."""
    _known(name)
    if not registry.chat_ready(name):
        return JSONResponse({"error": "這份報表還沒準備好問答（還在處理，或只有 Excel）"}, status_code=409)
    scheme, host = request.url.scheme, request.url.hostname
    try:
        port = await asyncio.to_thread(runner.chat_port, name, BIND["host"], str(request.base_url).rstrip("/"),
                                       f"{scheme}://{host}")
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return {"url": f"{scheme}://{host}:{port}/"}


@app.delete("/api/reports/{name}")
async def delete(name: str):
    _known(name)
    meta = registry.load_meta(name)
    if not meta:
        return JSONResponse({"error": "範例不能刪除"}, status_code=400)
    for child in [name, *meta.get("workbooks", [])]:
        runner.stop(child)
        shutil.rmtree(registry.WORKSPACE / child, ignore_errors=True)
        shutil.rmtree(ROOT / "data" / child, ignore_errors=True)
    if meta.get("parent") and registry.meta_path(meta["parent"]).exists():
        parent = registry.load_meta(meta["parent"])
        registry.save_meta(meta["parent"], workbooks=[w for w in parent.get("workbooks", []) if w != name])
    rebuild_map()
    return {"ok": True}


# ------------------------------------------------------------------------------------------- report map
MAP = {"status": "building", "data": None, "built_at": None, "error": ""}


def rebuild_map() -> None:
    """Build the catalog of every report and workbook (reads key columns: seconds to a few minutes)."""
    MAP["status"] = "building"
    try:
        from analysis import catalog
        from .catalog_view import view
        cat = catalog.build(catalog.load_reports(registry.names()))
        MAP.update(data=view(cat), status="ready", built_at=time.time(), error="")
    except Exception as e:  # noqa: BLE001 - shown on the map page
        MAP.update(status="failed", error=f"{type(e).__name__}: {e}")


jobs.on_finished.append(rebuild_map)


@app.on_event("startup")
async def _startup():
    model.start()
    threading.Thread(target=rebuild_map, daemon=True).start()


@app.get("/api/map")
async def report_map():
    return MAP


@app.post("/api/map/rebuild")
async def rebuild():
    if MAP["status"] != "building":
        threading.Thread(target=rebuild_map, daemon=True).start()
        MAP["status"] = "building"
    return {"status": MAP["status"]}
