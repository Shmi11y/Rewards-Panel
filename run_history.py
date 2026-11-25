"""Persist each task invocation and supervise the engine independently of the panel."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import windows_notifications

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    runner_pid INTEGER NOT NULL,
    engine_pid INTEGER,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    exit_code INTEGER,
    accounts TEXT NOT NULL DEFAULT '[]',
    points INTEGER,
    reason TEXT
)
"""
SOURCES = ("manual", "daily", "boot")
ACCOUNT_FAILURES = {"failed", "needs_login", "skipped"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BUSY_EXIT = 75


def process_alive(pid: int) -> bool:
    if not pid:
        return False
    kernel = ctypes.windll.kernel32
    open_process = kernel.OpenProcess
    open_process.restype = ctypes.c_void_p
    handle = open_process(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def _connect(data: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(data / "runs.sqlite3", timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _claim(data: Path, source: str) -> int | None:
    data.mkdir(parents=True, exist_ok=True)
    now = time.time()
    oldest = _history_start()
    with closing(_connect(data)) as conn, conn:
        conn.execute(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("SELECT id, runner_pid FROM runs WHERE status = 'running'").fetchone()
        if active:
            if process_alive(active["runner_pid"]):
                return None
            conn.execute("UPDATE runs SET status = 'interrupted', finished_at = ? WHERE id = ?", (now, active["id"]))
        conn.execute("DELETE FROM runs WHERE started_at < ? AND id != (SELECT MAX(id) FROM runs)", (oldest,))
        cursor = conn.execute(
            "INSERT INTO runs(source, started_at, status, runner_pid) VALUES (?, ?, 'running', ?)",
            (source, now, os.getpid()),
        )
        return cursor.lastrowid


def _view(row: sqlite3.Row) -> dict:
    status = row["status"]
    if status == "running" and not process_alive(row["runner_pid"]):
        status = "interrupted"
    return {
        "id": row["id"], "source": row["source"], "startedAt": row["started_at"],
        "finishedAt": row["finished_at"], "status": status, "exitCode": row["exit_code"],
        "accounts": json.loads(row["accounts"]), "points": row["points"], "reason": row["reason"],
    }


def _history_start() -> float:
    return datetime.combine(datetime.now().date() - timedelta(days=6), datetime.min.time()).timestamp()


def read(data: Path, *, latest: bool = False) -> list[dict]:
    if not (data / "runs.sqlite3").is_file():
        return []
    with closing(_connect(data)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'runs'").fetchone():
            return []
        if latest:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        else:
            rows = conn.execute("SELECT * FROM runs WHERE started_at >= ? ORDER BY id DESC", (_history_start(),))
        return [_view(row) for row in rows]


def launch(source: str, *, env: dict, output=subprocess.DEVNULL) -> subprocess.Popen | None:
    requested_at = time.time()
    data = Path(env["REWARDS_DATA_DIR"])
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), source],
        cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    deadline = time.monotonic() + 8
    while True:
        if (data / "runs.sqlite3").is_file():
            try:
                with closing(_connect(data)) as conn:
                    row = conn.execute(
                        "SELECT 1 FROM runs WHERE runner_pid = ? AND started_at >= ?",
                        (proc.pid, requested_at),
                    ).fetchone()
                if row:
                    return proc
            except sqlite3.OperationalError:
                pass  # The runner may be creating the database.
        if proc.poll() is not None:
            if proc.returncode == BUSY_EXIT:
                return None
            raise OSError("任务进程未能初始化运行记录")
        if time.monotonic() >= deadline:
            raise OSError("等待任务进程初始化超时")
        time.sleep(0.05)


def _stop_engine(pid: int) -> bool:
    try:
        killed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=8, creationflags=NO_WINDOW,
        )
        return killed.returncode == 0 or not process_alive(pid)
    except (OSError, subprocess.SubprocessError):
        return False


def request_stop(data: Path) -> dict | None:
    if not (data / "runs.sqlite3").is_file():
        return None
    with closing(_connect(data)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'runs'").fetchone():
            return None
        row = conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchone()
        if not row or not process_alive(row["runner_pid"]):
            return None
        conn.execute("UPDATE runs SET stop_requested = 1 WHERE id = ?", (row["id"],))
    if row["engine_pid"] and not _stop_engine(row["engine_pid"]):
        with closing(_connect(data)) as conn, conn:
            conn.execute("UPDATE runs SET stop_requested = 0 WHERE id = ? AND status = 'running'", (row["id"],))
        return {"ok": False, "msg": "结束进程失败"}
    return {"ok": True, "stopped": row["engine_pid"] or row["runner_pid"]}


def _results(events: Path, code: int) -> tuple[str, list[dict], int | None]:
    accounts: dict[str, dict] = {}
    login: set[str] = set()
    ended = failed = skipped = False

    def account(email: str) -> dict:
        return accounts.setdefault(email.casefold(), {"email": email, "status": "pending", "points": None})

    for path in events.glob("*.jsonl"):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event["type"]
                if kind == "accounts":
                    for email in event["emails"]:
                        account(email)
                elif kind == "run_ended":
                    ended, failed = True, event["failed"]
                elif kind == "run_skipped":
                    skipped = True
                else:
                    row = account(event["email"])
                    if kind == "login":
                        login.add(row["email"].casefold())
                    elif kind == "login_ok":
                        login.discard(row["email"].casefold())
                    elif kind == "started":
                        row["status"] = "started"
                    else:
                        row["status"] = "failed" if kind == "partial" else kind
                        if event.get("points") is not None:
                            row["points"] = event["points"]

    for key, row in accounts.items():
        if skipped:
            row["status"] = "not_run"
        elif key in login and row["status"] in {"failed", "started"}:
            row["status"] = "needs_login"
        elif row["status"] == "pending":
            row["status"] = "not_run"
        elif row["status"] == "started":
            row["status"] = "interrupted" if code else "unknown"

    rows = list(accounts.values())
    statuses = {row["status"] for row in rows}
    points = [row["points"] for row in rows if row["points"] is not None]
    if skipped:
        status = "skipped"
    elif code != 0 or failed or statuses & ACCOUNT_FAILURES:
        status = "partial" if "success" in statuses and len(statuses) > 1 else "failed"
    elif ended and statuses == {"success"}:
        status = "success"
    else:
        status = "unknown"
    return status, rows, sum(points) if points else None


def _finish(data: Path, run_id: int, status: str, accounts: list[dict], points: int | None,
            code: int, reason: str | None) -> str:
    with closing(_connect(data)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT stop_requested FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row["stop_requested"]:
            status, reason = "stopped", None
            for account in accounts:
                if account["status"] in {"unknown", "interrupted"}:
                    account["status"] = "stopped"
        conn.execute(
            """UPDATE runs SET status = ?, finished_at = ?, accounts = ?, points = ?, exit_code = ?,
               reason = ? WHERE id = ?""",
            (status, time.time(), json.dumps(accounts, ensure_ascii=False), points, code, reason, run_id),
        )
    return status


def _notify(data: Path, source: str, status: str, accounts: list[dict]) -> None:
    if status not in {"failed", "partial"}:
        return
    login_count = sum(account["status"] == "needs_login" for account in accounts)
    source_label = {"manual": "手动任务", "daily": "定时任务", "boot": "开机任务"}[source]
    title = "Rewards 需要重新登录" if login_count else "Rewards 运行失败"
    result = f"{login_count} 个账号需要重新登录" if login_count else ("部分账号未完成" if status == "partial" else "运行未完成")
    try:
        windows_notifications.show(ROOT, title, f"{source_label}：{result}。详情见 Rewards 面板的运行记录。")
    except Exception as exc:  # The result is already committed, independently of Windows delivery.
        try:
            with (data / "notifications.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {exc}\n")
        except OSError:
            pass


def run(data: Path, source: str) -> int:
    run_id = _claim(data, source)
    if run_id is None:
        return BUSY_EXIT
    code, reason = 1, None
    status, accounts, points = "failed", [], None
    try:
        (data / "task.pid").write_text(str(os.getpid()), encoding="utf-8")
        with TemporaryDirectory(prefix="run-events-", dir=data) as temp:
            env = dict(os.environ, REWARDS_DATA_DIR=str(data), REWARDS_APP_ROOT=str(ROOT),
                       PLAYWRIGHT_BROWSERS_PATH=str(ROOT / "browser"), REWARDS_RUN_EVENTS=temp,
                       NODE_NO_WARNINGS="1")
            node, entry, preload = ROOT / "app/node.exe", ROOT / "app/dist/index.js", ROOT / "task-data.cjs"
            missing = next((path for path in (node, entry, preload) if not path.is_file()), None)
            if missing:
                reason = f"缺少 {missing.relative_to(ROOT).as_posix()}"
            else:
                flags = 0 if sys.stdin is not None and sys.stdin.isatty() else NO_WINDOW
                proc = subprocess.Popen([str(node), "--require", str(preload), str(entry)],
                                        cwd=ROOT / "app", env=env, creationflags=flags)
                with closing(_connect(data)) as conn, conn:
                    conn.execute("UPDATE runs SET engine_pid = ? WHERE id = ?", (proc.pid, run_id))
                    stop = conn.execute("SELECT stop_requested FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
                if stop:
                    _stop_engine(proc.pid)
                code = proc.wait()
                status, accounts, points = _results(Path(temp), code)
                if status in {"failed", "partial"} and not accounts:
                    reason = f"引擎退出，未返回账号结果（退出码 {code}）"
    except OSError:
        reason = "无法启动引擎或读取运行结果"
    status = _finish(data, run_id, status, accounts, points, code, reason)
    _notify(data, source, status, accounts)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=(*SOURCES, "auto"))
    options = parser.parse_args()
    data = Path(os.environ.get("REWARDS_DATA_DIR") or ROOT / "data").resolve()
    source = options.source
    if source == "auto":
        try:
            schedule = json.loads((data / "schedule.json").read_text(encoding="utf-8"))
            source = "boot" if schedule.get("mode") == "boot" else "manual"
        except (OSError, ValueError, AttributeError):
            source = "manual"
    raise SystemExit(run(data, source))
