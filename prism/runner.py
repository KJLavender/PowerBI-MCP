"""One chat server per report (python -m agent.web), started when someone opens the report in PRISM.

Each report keeps its own analysis context, snapshots and conversations; the local model behind them is shared
(Ollama loads it once). Servers keep running until PRISM stops.
"""
import atexit
import os
import socket
import subprocess
import sys
import time
import urllib.request

from analysis.dataset import ROOT

from . import registry

FIRST_PORT = int(os.environ.get("PRISM_CHAT_PORTS", "8101"))
SERVERS: dict[str, tuple[int, subprocess.Popen]] = {}


def _free_port() -> int:
    used = {p for p, _ in SERVERS.values()}
    port = FIRST_PORT
    while True:
        if port not in used:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        port += 1


def _up(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=3)
        return True
    except Exception:  # noqa: BLE001
        return False


def chat_port(name: str, host: str, portal: str, public_host: str, wait: int = 180) -> int:
    """The port of the report's chat server, starting it if needed (the first start loads the model)."""
    if name in SERVERS:
        port, proc = SERVERS[name]
        if proc.poll() is None:
            return port
        SERVERS.pop(name)
    port = _free_port()
    meta = registry.load_meta(name) or registry.EXAMPLES.get(name, {})
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PBI_DATASET": name, "PRISM_URL": portal,
           "REPORT_URL": meta.get("report_url", ""), "PUBLIC_URL": f"{public_host}:{port}"}
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    out = open(logs / f"chat-{name}.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "agent.web", "--host", host, "--port", str(port)],
                            cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    SERVERS[name] = (port, proc)
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            SERVERS.pop(name, None)
            raise RuntimeError(f"問答服務沒有啟動，請看 logs/chat-{name}.log")
        if _up(port):
            return port
        time.sleep(1)
    raise RuntimeError("問答服務啟動逾時（第一次要載入模型，請稍後再試）")


def stop(name: str) -> None:
    if name in SERVERS:
        _, proc = SERVERS.pop(name)
        proc.terminate()
        try:  # its files stay locked on Windows until it has exited
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@atexit.register
def _stop_all() -> None:
    for name in list(SERVERS):
        stop(name)
