"""Chat history kept on this machine (data/chats.sqlite, outside git).

Each turn stores what the page shows (question, answer, check result, tool calls, focus) plus the tool
results, so a conversation reopened later — even after a server restart — keeps working for follow-ups
and its numbers stay checkable.

Sharing: a share link (random token, 30 days) points at a conversation and the report view it was asked on.
Whoever opens it gets their own copy of the conversation (a fork) to continue; the original stays as it was.
"""
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from analysis.dataset import DATA_DIR

from .host import Turn

DB = DATA_DIR / "chats.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS turns (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, created_at REAL NOT NULL,
    question TEXT NOT NULL, answer TEXT NOT NULL, grounded INTEGER NOT NULL,
    retries INTEGER NOT NULL, seconds REAL NOT NULL,
    tools TEXT NOT NULL, evidence TEXT NOT NULL, focus TEXT, context TEXT,
    PRIMARY KEY (conversation_id, idx));
CREATE TABLE IF NOT EXISTS shares (
    token TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
    sender TEXT, context TEXT, data_date TEXT);
"""
SHARE_DAYS = 30


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    if "checks" not in {r[1] for r in con.execute("PRAGMA table_info(turns)")}:
        con.execute("ALTER TABLE turns ADD COLUMN checks TEXT")  # per-check result, added later
    if "shared_from" not in {r[1] for r in con.execute("PRAGMA table_info(conversations)")}:
        con.execute("ALTER TABLE conversations ADD COLUMN shared_from TEXT")  # share token of a fork
    return con


def save_turn(conversation_id: str, turn: Turn, focus: dict | None, context: dict | None) -> None:
    now = time.time()
    with connect() as con:
        title = turn.question.strip().splitlines()[0][:60]
        con.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
                    (conversation_id, title, now, now))
        idx = con.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM turns WHERE conversation_id = ?",
                          (conversation_id,)).fetchone()[0]
        con.execute("INSERT INTO turns (conversation_id, idx, created_at, question, answer, grounded, retries, "
                    "seconds, tools, evidence, focus, context, checks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            conversation_id, idx, now, turn.question, turn.answer, int(not turn.ungrounded), turn.retries,
            round(turn.seconds, 1), json.dumps(turn.tool_calls, ensure_ascii=False),
            json.dumps(turn.evidence, ensure_ascii=False),
            json.dumps(focus, ensure_ascii=False) if focus else None,
            json.dumps(context, ensure_ascii=False) if context else None,
            json.dumps(turn.check, ensure_ascii=False) if turn.check else None))


def list_conversations(limit: int = 100) -> list[dict]:
    with connect() as con:
        rows = con.execute("""SELECT c.id, c.title, c.updated_at, COUNT(t.idx) AS turns
                              FROM conversations c LEFT JOIN turns t ON t.conversation_id = c.id
                              GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def load_turns(conversation_id: str) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM turns WHERE conversation_id = ? ORDER BY idx", (conversation_id,)).fetchall()
    out = []
    for r in rows:
        out.append({"question": r["question"], "answer": r["answer"], "grounded": bool(r["grounded"]),
                    "retries": r["retries"], "seconds": r["seconds"], "created_at": r["created_at"],
                    "tools": json.loads(r["tools"]), "evidence": json.loads(r["evidence"]),
                    "focus": json.loads(r["focus"]) if r["focus"] else None,
                    "check": json.loads(r["checks"]) if r["checks"] else None})
    return out


def restore_history(conversation_id: str) -> list[Turn]:
    """Rebuild Turn objects so follow-up questions in a reopened conversation keep their context."""
    turns = []
    for t in load_turns(conversation_id)[-6:]:
        turn = Turn(t["question"], t["answer"], tool_calls=t["tools"], retries=t["retries"], seconds=t["seconds"])
        turn.evidence = t["evidence"]
        turn.check = t["check"] or {}
        turns.append(turn)
    return turns


def delete_conversation(conversation_id: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))
        con.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


# ---------------------------------------------------------------------------------- sharing
def create_share(conversation_id: str, context: dict | None, sender: str | None, data_date: str | None) -> str:
    """A link token for the conversation as it is now (later turns of the original are not included)."""
    token = secrets.token_urlsafe(16)
    now = time.time()
    with connect() as con:
        if not con.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
            raise KeyError(conversation_id)
        n = con.execute("SELECT COUNT(*) FROM turns WHERE conversation_id = ?", (conversation_id,)).fetchone()[0]
        con.execute("INSERT INTO shares (token, conversation_id, created_at, expires_at, sender, context, data_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (token, conversation_id, now, now + SHARE_DAYS * 86400, sender,
                     json.dumps({**(context or {}), "_turns": n}, ensure_ascii=False), data_date))
    return token


def get_share(token: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
    if not r or r["expires_at"] < time.time():
        return None
    out = dict(r)
    out["context"] = json.loads(r["context"]) if r["context"] else {}
    return out


def fork(token: str) -> str | None:
    """A new conversation with the shared turns, for the person who opened the link to continue."""
    share = get_share(token)
    if share is None:
        return None
    new_id = str(uuid.uuid4())
    upto = share["context"].get("_turns", 10 ** 6)
    now = time.time()
    with connect() as con:
        title = con.execute("SELECT title FROM conversations WHERE id = ?", (share["conversation_id"],)).fetchone()
        con.execute("INSERT INTO conversations (id, title, created_at, updated_at, shared_from) VALUES (?, ?, ?, ?, ?)",
                    (new_id, f"（轉寄）{title['title'] if title else ''}", now, now, token))
        con.execute("""INSERT INTO turns (conversation_id, idx, created_at, question, answer, grounded, retries,
                       seconds, tools, evidence, focus, context, checks)
                       SELECT ?, idx, created_at, question, answer, grounded, retries, seconds, tools, evidence,
                              focus, context, checks
                       FROM turns WHERE conversation_id = ? AND idx < ?""", (new_id, share["conversation_id"], upto))
    return new_id


def conversation_meta(conversation_id: str) -> dict:
    """Where a conversation came from (a share), for the banner on a forwarded conversation."""
    with connect() as con:
        r = con.execute("SELECT shared_from FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        share = con.execute("SELECT * FROM shares WHERE token = ?", (r["shared_from"],)).fetchone() \
            if r and r["shared_from"] else None
    if not share:
        return {}
    return {"shared": True, "sender": share["sender"], "shared_at": share["created_at"], "data_date": share["data_date"]}
