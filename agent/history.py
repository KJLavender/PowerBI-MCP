"""Chat history kept on this machine (data/chats.sqlite, outside git).

Each turn stores what the page shows (question, answer, check result, tool calls, focus) plus the tool
results, so a conversation reopened later — even after a server restart — keeps working for follow-ups
and its numbers stay checkable.
"""
import json
import sqlite3
import time
from pathlib import Path

from .host import ROOT, Turn

DB = ROOT / "data" / "chats.sqlite"

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
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
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
        con.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            conversation_id, idx, now, turn.question, turn.answer, int(not turn.ungrounded), turn.retries,
            round(turn.seconds, 1), json.dumps(turn.tool_calls, ensure_ascii=False),
            json.dumps(turn.evidence, ensure_ascii=False),
            json.dumps(focus, ensure_ascii=False) if focus else None,
            json.dumps(context, ensure_ascii=False) if context else None))


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
                    "focus": json.loads(r["focus"]) if r["focus"] else None})
    return out


def restore_history(conversation_id: str) -> list[Turn]:
    """Rebuild Turn objects so follow-up questions in a reopened conversation keep their context."""
    turns = []
    for t in load_turns(conversation_id)[-6:]:
        turn = Turn(t["question"], t["answer"], tool_calls=t["tools"], retries=t["retries"], seconds=t["seconds"])
        turn.evidence = t["evidence"]
        turns.append(turn)
    return turns


def delete_conversation(conversation_id: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))
        con.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
