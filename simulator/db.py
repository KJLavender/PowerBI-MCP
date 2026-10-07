import os
from pathlib import Path

import oracledb

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def connect() -> oracledb.Connection:
    load_env()
    return oracledb.connect(
        user=os.environ.get("ORACLE_USER", "SALES_DW"),
        password=os.environ["ORACLE_PASSWORD"],
        # 127.0.0.1, not localhost: WSL doesn't forward ::1, and IPv6-first resolution stalls connects
        dsn=os.environ.get("ORACLE_DSN", "127.0.0.1:1521/FREEPDB1"),
    )


def run_script(cur: oracledb.Cursor, path: Path) -> None:
    """Execute a SQL file whose statements are separated by a line with only '/'."""
    text = path.read_text(encoding="utf-8")
    for stmt in text.split("\n/\n"):
        stmt = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if stmt:
            cur.execute(stmt)
