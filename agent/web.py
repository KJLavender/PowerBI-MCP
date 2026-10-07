"""Local chat page for the report agent.

    python -m agent.web [--port 8090] [--host 127.0.0.1]

One Agent (MCP server + local model) is shared by all browser tabs; questions are answered one at a
time because they share a single local GPU. Each tab keeps its own conversation for follow-ups.
"""
import argparse
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .host import DEFAULT_MODEL, Agent, Turn

STATIC = Path(__file__).with_name("static")
EXAMPLES = [
    "最近一天的報告有什麼需要注意的？",
    "為什麼 2026-09-17 北區的銷售額下降？",
    "截至 2026-08-03 的近 28 日，生鮮食品的毛利率為什麼下降？",
    "2026-10-03 高雄左營店的業績為什麼大跌？",
    "以 2026-08-23 為準，VIP 顧客的訂單數跟去年同期比，是不是在流失？",
    "客單價是怎麼計算的？資料從哪裡來？",
]


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


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
async def info():
    return {"model": State.model, "latest": State.agent.latest, "examples": EXAMPLES,
            "metrics": State.agent.metrics}


@app.post("/api/reset")
async def reset(request: Request):
    body = await request.json()
    State.conversations.pop(body.get("session", ""), None)
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
                history = State.conversations.setdefault(session, [])
                turn = await State.agent.ask(question, history=history, on_event=on_event)
                history.append(turn)
                del history[:-6]
                await queue.put({"type": "answer", "text": turn.answer, "grounded": not turn.ungrounded,
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
