"""Local chat page for the report agent.

    python -m agent.web [--port 8090] [--host 127.0.0.1]

One Agent (MCP server + local model) is shared by all browser tabs; questions are answered one at a
time because they share a single local GPU. Each tab keeps its own conversation for follow-ups.
"""
import argparse
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

from analysis import dataset, tools
from analysis.visuals import REPORT_SPAN

from . import history as history_store
from .host import DEFAULT_MODEL, HINTS, Agent, Turn

STATIC = Path(__file__).with_name("static")
EXAMPLES = HINTS.get("sample_questions", [])


class State:
    agent: Agent | None = None
    lock = asyncio.Lock()
    conversations: dict[str, list[Turn]] = {}
    model = DEFAULT_MODEL


@asynccontextmanager
async def lifespan(app: FastAPI):
    State.agent = await Agent(State.model).__aenter__()
    try:
        yield
    finally:
        await State.agent.__aexit__(None, None, None)


app = FastAPI(lifespan=lifespan)


# the published report next to the chat; datasets that are not on Report Server get the chat page only
# links put in mail must open on the colleague's machine: set PUBLIC_URL to this server's address on the network
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
PRISM_URL = os.environ.get("PRISM_URL", "")  # the PRISM portal that started this chat, for the way back
REPORT_URL = os.environ.get("REPORT_URL", "http://localhost:8081/Reports/powerbi/RetailSales"
                            if dataset.NAME == "retail" else "")


@app.get("/")
async def report_with_chat():
    """Report Server report on the left, chat panel on the right."""
    if not REPORT_URL:
        return RedirectResponse("/chat")
    return FileResponse(STATIC / "report.html")


@app.get("/chat")
async def chat_page():
    return FileResponse(STATIC / "index.html")


def latest_date() -> str | None:
    """Latest loaded business day, read live: the server runs for days while the daily job adds data."""
    with tools.store.open_db(read_only=True) as db:
        last = tools.store.last_snapshot_date(db)
    return str(last) if last else None


@app.get("/api/info")
async def info():
    sep = "&" if "?" in REPORT_URL else "?"
    return {"model": State.model, "latest": latest_date() or State.agent.latest, "examples": EXAMPLES,
            "metrics": State.agent.metrics, "report_url": REPORT_URL,
            "report_embed_url": f"{REPORT_URL}{sep}rs:embed=true", "portal": PRISM_URL,
            "report": dataset.NAME}


@app.get("/api/visuals")
async def visuals_list():
    """The report's pages and visuals (from its layout), so a chat without the report next to it can still
    point at a visual."""
    try:
        _, _, infos = tools.report_visuals()
    except Exception:  # noqa: BLE001 - a dataset without a report layout simply has no list
        return []
    pages: dict[str, list] = {}
    for v in infos:
        if not v.hidden_page:
            pages.setdefault(v.page, []).append({"title": v.title or v.kind, "kind": v.kind,
                                                 "author": bool([e for e in v.emphasis if e.explicit])})
    return [{"page": p, "visuals": vs} for p, vs in pages.items()]


@app.get("/api/context-options")
async def context_options():
    """Selectors for the shared filter bar: dimensions with members and the model column each one filters
    in the report (Table/Column for Report Server URL filters)."""
    c = tools.ctx()
    dims = []
    for k, d in c.dims.items():
        members = tools.list_members(d.label)["成員（依近 28 日交易量排序）"]
        dims.append({"key": k, "label": d.label, "field": f"{d.table}/{d.column}", "members": sorted(members)})
    with tools.store.open_db(read_only=True) as db:
        first, last = db.execute("SELECT min(business_date), max(business_date) FROM snapshot_log").fetchone()
    date_dim = c.date_dim
    return {"dims": dims, "date_field": f"{date_dim.table}/{date_dim.column}", "min": str(first), "max": str(last),
            "windows": {k: v["label"] for k, v in c.config["windows"].items()},
            "spans": REPORT_SPAN}


def focus_of(turn: Turn) -> dict | None:
    """The scope the answer was about (from its last analysis tool call) + the report page that shows it."""
    c = tools.ctx()
    by_key = {k: d.label for k, d in c.dims.items()}
    for call in reversed(turn.tool_calls):
        if call["tool"] not in ("explain_change", "metric_change", "metric_trend", "data_quality"):
            continue
        args = call["args"]
        filters = args.get("filters") or {}
        if isinstance(filters, str):
            try:
                filters = json.loads(filters) if filters.strip() else {}
            except json.JSONDecodeError:
                filters = {}
        filters = {by_key.get(k, k): v for k, v in filters.items()}
        date = args.get("date") or args.get("end")
        try:
            d, _ = tools.parse_period(date)
        except tools.ToolError:
            d = None
        window = args.get("window", "day") if call["tool"] != "data_quality" else "day"
        return {"filters": filters, "date": str(tools.clamp_to_data(d)) if d else None,
                "month": bool(date and tools.MONTH_ONLY.match(date)), "window": window,
                "metric": args.get("metric"), "page": best_page(filters, call["tool"])}
    return None


def best_page(filters: dict, tool: str) -> str | None:
    """Report page whose visuals use the most of the focus dimensions (read from the PBIR report)."""
    c = tools.ctx()
    if tool == "data_quality":
        wanted = {f"{c.dq['etl_table']}."} if c.dq.get("etl_table") else set()
    else:
        wanted = {f"{d.table}.{d.column}" for d in c.dims.values() if d.label in filters}
    if not wanted:
        return None
    scores: dict[str, int] = {}
    for u in c.model.usage:
        hits = sum(1 for col in u.columns for w in wanted if col.startswith(w) or col == w)
        scores[u.page] = scores.get(u.page, 0) + hits
    page, score = max(scores.items(), key=lambda kv: kv[1], default=(None, 0))
    return page if score else None


def context_text(ctx: dict | None) -> str | None:
    if not ctx:
        return None
    parts = [f"{k}={v}" for k, v in (ctx.get("filters") or {}).items() if v]
    if ctx.get("date"):
        parts.append(f"date={ctx['date']}")
    if not parts:
        return None  # nothing selected: don't nudge the model with just the default window
    if ctx.get("window"):
        parts.append(f"window={ctx['window']}")
    return "、".join(parts)


@app.get("/api/conversations")
async def conversations():
    return history_store.list_conversations()


@app.get("/api/conversations/{conversation_id}")
async def conversation(conversation_id: str):
    turns = history_store.load_turns(conversation_id)
    for t in turns:  # the page needs what it showed, not the raw evidence
        t.pop("evidence", None)
        t["tools"] = [{"name": c["tool"], "args": c["args"], "result": c.get("result", "")} for c in t["tools"]]
    return turns


@app.get("/api/conversations/{conversation_id}/meta")
async def conversation_meta(conversation_id: str):
    meta = history_store.conversation_meta(conversation_id)
    if meta:
        meta["latest"] = latest_date()  # newer data than the shared answers were computed on?
    return meta


@app.post("/api/conversations/{conversation_id}/share")
async def share(conversation_id: str, request: Request):
    """A link that opens the same report view with a copy of this conversation, plus a mail draft."""
    body = await request.json()
    try:
        token = history_store.create_share(conversation_id, body.get("context"), body.get("sender"), latest_date())
    except KeyError:
        return JSONResponse({"error": "這個對話還沒有任何問答，沒有東西可以轉寄"}, status_code=404)
    base = (PUBLIC_URL or str(request.base_url)).rstrip("/")
    url = f"{base}/s/{token}"
    turns = history_store.load_turns(conversation_id)
    last = turns[-1]
    conclusion = next((line for line in last["answer"].splitlines() if line.strip()), "")
    conclusion = conclusion.replace("**", "").replace("結論：", "").strip()[:300]
    ctx = body.get("context") or {}
    windows = {k: v["label"] for k, v in tools.ctx().config["windows"].items()}
    view = "、".join([f"{k}：{v}" for k, v in (ctx.get("filters") or {}).items() if v]
                     + ([f"日期 {ctx['date']}（{windows.get(ctx.get('window'), ctx.get('window') or '')}）"]
                        if ctx.get("date") else []))
    asked = "\n".join(f"{i}. {t['question']}" for i, t in enumerate(turns[:5], 1)) + \
        (f"\n…共 {len(turns)} 個問題" if len(turns) > 5 else "")
    subject = f"[報表問答] {turns[0]['question'][:40]}"
    text = (f"我在報表問答問了：\n{asked}\n\n最後的結論：{conclusion}\n\n"
            + (f"當時報表的篩選：{view}\n" if view else "當時報表沒有篩選（全部期間）\n")
            + f"\n點下面的連結，會打開同一個報表畫面和這段對話，你可以直接接著問：\n{url}\n\n"
            f"（資料截至 {latest_date()}；連結 {history_store.SHARE_DAYS} 天內有效）")
    return {"url": url, "subject": subject, "body": text}


@app.get("/s/{token}")
async def open_share(token: str):
    """Fork the shared conversation and open it on the report view it was asked on."""
    share = history_store.get_share(token)
    new_id = history_store.fork(token) if share else None
    if new_id is None:
        return JSONResponse({"error": "這個連結已過期或不存在"}, status_code=404)
    view = {k: v for k, v in (share["context"] or {}).items() if not k.startswith("_")}
    target = "/" if REPORT_URL else "/chat"
    hash_ = "#" + quote(json.dumps(view, ensure_ascii=False)) if view else ""
    return RedirectResponse(f"{target}?session={new_id}{hash_}")


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    history_store.delete_conversation(conversation_id)
    State.conversations.pop(conversation_id, None)
    return {"ok": True}


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    question = (body.get("question") or "").strip()
    session = body.get("session") or str(uuid.uuid4())
    if not question:
        return JSONResponse({"error": "請輸入問題"}, status_code=400)
    queue: asyncio.Queue = asyncio.Queue()

    async def on_event(event: dict) -> None:
        await queue.put(event)

    async def work() -> None:
        try:
            if State.lock.locked():
                await queue.put({"type": "status", "text": "前一個問題還在處理，排隊中…"})
            async with State.lock:
                await queue.put({"type": "status", "text": "思考中"})
                State.agent.latest = latest_date() or State.agent.latest  # the daily job may have added a day
                if session not in State.conversations:  # reopened conversation / server restarted
                    State.conversations[session] = history_store.restore_history(session)
                history = State.conversations[session]
                turn = await State.agent.ask(question, history=history, on_event=on_event,
                                             context=context_text(body.get("context")))
                history.append(turn)
                del history[:-6]
                focus = focus_of(turn) if not turn.ungrounded else None
                history_store.save_turn(session, turn, focus, body.get("context"))
                await queue.put({"type": "answer", "text": turn.answer, "grounded": not turn.ungrounded,
                                 "check": turn.check,
                                 "focus": focus, "session": session,
                                 "retries": turn.retries, "seconds": round(turn.seconds, 1),
                                 "tools": [{"name": c["tool"], "args": c["args"], "result": c.get("result", "")}
                                           for c in turn.tool_calls]})
        except Exception as e:  # report the failure to the page instead of hanging the stream
            await queue.put({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(work())
        while (event := await queue.get()) is not None:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        await task

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main() -> None:
    p = argparse.ArgumentParser(prog="agent.web")
    p.add_argument("--host", default="127.0.0.1", help="預設只開放本機；開放區網請改 0.0.0.0（資料會在區網內傳輸）")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args()
    State.model = args.model
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
