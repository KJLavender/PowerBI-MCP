"""The local model the chats use (Ollama): is it running, is the model there, download it if not.

Checked when PRISM starts so a new installation says what is missing instead of failing at the first question.
"""
import json
import threading
import urllib.request

from agent.host import DEFAULT_MODEL, OLLAMA_HOST

STATUS = {"state": "checking", "model": DEFAULT_MODEL, "host": OLLAMA_HOST, "detail": ""}


def _get(path: str):
    with urllib.request.urlopen(f"{OLLAMA_HOST}{path}", timeout=5) as r:
        return json.load(r)


def _pull() -> None:
    req = urllib.request.Request(f"{OLLAMA_HOST}/api/pull", data=json.dumps({"model": DEFAULT_MODEL}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=None) as r:
        for line in r:
            msg = json.loads(line)
            if msg.get("error"):
                raise RuntimeError(msg["error"])
            if msg.get("total"):
                STATUS["detail"] = f"{msg.get('completed', 0) / msg['total']:.0%}"


def check() -> None:
    try:
        tags = _get("/api/tags")
    except Exception:  # noqa: BLE001
        STATUS.update(state="no_ollama", detail="")
        return
    names = {m["name"] for m in tags.get("models", [])}
    if DEFAULT_MODEL in names or f"{DEFAULT_MODEL}:latest" in names:
        STATUS.update(state="ready", detail="")
        return
    STATUS.update(state="pulling", detail="0%")
    try:
        _pull()
        STATUS.update(state="ready", detail="")
    except Exception as e:  # noqa: BLE001
        STATUS.update(state="pull_failed", detail=str(e))


def start() -> None:
    threading.Thread(target=check, daemon=True).start()
