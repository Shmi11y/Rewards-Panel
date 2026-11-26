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
from collections import deque
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
    reason TEXT,
    scheduled_at INTEGER,
    schedule_kind TEXT
)
"""
SOURCES = ("manual", "daily", "boot")
LOGIN_FAILURES_SCHEMA = """
CREATE TABLE account_login_failures (
    email TEXT PRIMARY KEY,
    required_at REAL NOT NULL
)
"""
ACCOUNT_FAILURES = {"failed", "needs_login", "skipped"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BUSY_EXIT = 75
RUN_LOG_LIMIT = 512 * 1024
MAX_PAUSE_TIMESTAMP = 32_503_680_000


def _log_file(data: Path, run_id: int) -> Path:
    return data / "run-logs" / f"{run_id}.log"


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


def _begin_write(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "scheduled_at" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN scheduled_at INTEGER")
    if "schedule_kind" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN schedule_kind TEXT")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS runs_scheduled_slot
                    ON runs(scheduled_at) WHERE schedule_kind != 'retry'""")
    if not _has_login_failures_table(conn):
        conn.execute(LOGIN_FAILURES_SCHEMA)
        _store_login_failures(conn, _historical_login_failures(conn))


def _has_login_failures_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'account_login_failures'").fetchone() is not None


def _login_failures(accounts: list[dict], finished_at: float) -> dict[str, float]:
    return {row["email"].casefold(): row.get("loginRequiredAt", finished_at * 1000)
            for row in accounts if row["status"] == "needs_login"}


def _historical_login_failures(conn: sqlite3.Connection) -> dict[str, float]:
    failures: dict[str, float] = {}
    rows = conn.execute("""SELECT accounts, finished_at FROM runs
                           WHERE finished_at IS NOT NULL AND accounts LIKE '%"needs_login"%'""")
    for row in rows:
        try:
            accounts = json.loads(row["accounts"])
            for email, required_at in _login_failures(accounts, row["finished_at"]).items():
                failures[email] = max(required_at, failures.get(email, 0))
        except (TypeError, ValueError, KeyError):
            continue
    return failures


def _store_login_failures(conn: sqlite3.Connection, failures: dict[str, float]) -> None:
    conn.executemany(
        """INSERT INTO account_login_failures(email, required_at) VALUES (?, ?)
           ON CONFLICT(email) DO UPDATE SET required_at = MAX(required_at, excluded.required_at)""",
        failures.items(),
    )


def account_login_failures(data: Path) -> dict[str, float]:
    if not (data / "runs.sqlite3").is_file():
        return {}
    try:
        with closing(_connect(data)) as conn:
            conn.execute("BEGIN")
            if _has_login_failures_table(conn):
                return dict(conn.execute("SELECT email, required_at FROM account_login_failures"))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'runs'").fetchone():
                return _historical_login_failures(conn)
    except sqlite3.Error:
        pass
    return {}


def processed_slots(data: Path, since: int) -> set[int]:
    if not (data / "runs.sqlite3").is_file():
        return set()
    with closing(_connect(data)) as conn:
        if not any(row["name"] == "scheduled_at" for row in conn.execute("PRAGMA table_info(runs)")):
            return set()
        rows = conn.execute(
            "SELECT scheduled_at FROM runs WHERE scheduled_at >= ? AND schedule_kind != 'retry'", (since,)
        )
        return {row[0] for row in rows}


def record_skips(data: Path, skips: list[tuple[int, str]]) -> None:
    if not skips:
        return
    data.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with closing(_connect(data)) as conn, conn:
        _begin_write(conn)
        conn.executemany(
            """INSERT OR IGNORE INTO runs(source, started_at, finished_at, status, runner_pid,
                   reason, scheduled_at, schedule_kind)
               VALUES ('daily', ?, ?, 'skipped', 0, ?, ?, 'skipped')""",
            [(now, now, reason, slot) for slot, reason in skips],
        )


def _claim(data: Path, source: str, scheduled_at: int | None, schedule_kind: str | None) -> int | None:
    data.mkdir(parents=True, exist_ok=True)
    now = time.time()
    oldest = _history_start()
    with closing(_connect(data)) as conn, conn:
        _begin_write(conn)
        if scheduled_at is not None and schedule_kind != "retry" and conn.execute(
            "SELECT 1 FROM runs WHERE scheduled_at = ? AND schedule_kind != 'retry'", (scheduled_at,)
        ).fetchone():
            return None
        active = conn.execute("SELECT id, runner_pid FROM runs WHERE status = 'running'").fetchone()
        if active:
            if process_alive(active["runner_pid"]):
                return None
            conn.execute("UPDATE runs SET status = 'interrupted', finished_at = ? WHERE id = ?", (now, active["id"]))
        expired = conn.execute(
            "DELETE FROM runs WHERE started_at < ? AND id != (SELECT MAX(id) FROM runs) RETURNING id", (oldest,)
        ).fetchall()
        cursor = conn.execute(
            """INSERT INTO runs(source, started_at, status, runner_pid, scheduled_at, schedule_kind)
               VALUES (?, ?, 'running', ?, ?, ?)""",
            (source, now, os.getpid(), scheduled_at, schedule_kind),
        )
        run_id = cursor.lastrowid
    for row in expired:
        try:
            _log_file(data, row["id"]).unlink(missing_ok=True)
        except OSError:
            pass
    return run_id


def _view(row: sqlite3.Row) -> dict:
    status = row["status"]
    if status == "running" and not process_alive(row["runner_pid"]):
        status = "interrupted"
    fields = row.keys()
    return {
        "id": row["id"], "source": row["source"], "startedAt": row["started_at"],
        "finishedAt": row["finished_at"], "status": status, "exitCode": row["exit_code"],
        "accounts": json.loads(row["accounts"]), "points": row["points"], "reason": row["reason"],
        "scheduledAt": row["scheduled_at"] if "scheduled_at" in fields else None,
        "scheduleKind": row["schedule_kind"] if "schedule_kind" in fields else None,
    }


def _history_start() -> float:
    return datetime.combine(datetime.now().date() - timedelta(days=6), datetime.min.time()).timestamp()


def read(data: Path, *, latest: bool = False, run_id: int | None = None) -> list[dict]:
    if not (data / "runs.sqlite3").is_file():
        return []
    with closing(_connect(data)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'runs'").fetchone():
            return []
        if run_id is not None:
            rows = conn.execute("SELECT * FROM runs WHERE id = ? AND started_at >= ?", (run_id, _history_start()))
        elif latest:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        else:
            rows = conn.execute("SELECT * FROM runs WHERE started_at >= ? ORDER BY id DESC", (_history_start(),))
        return [_view(row) for row in rows]


def read_log(data: Path, run: dict, daily_logs: Path) -> dict:
    path = _log_file(data, run["id"])
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - RUN_LOG_LIMIT))
            raw = stream.read(RUN_LOG_LIMIT)
        truncated = size > RUN_LOG_LIMIT
        if truncated and b"\n" in raw:
            raw = raw.split(b"\n", 1)[1]
        return {"text": raw.decode("utf-8", errors="replace"), "truncated": truncated, "legacy": False}
    except FileNotFoundError:
        pass

    result = {"text": "", "truncated": False, "legacy": True}
    if run["scheduleKind"] == "skipped" or run["finishedAt"] is None:
        return result
    start = datetime.fromtimestamp(run["startedAt"])
    end = datetime.fromtimestamp(run["finishedAt"])
    first = start.isoformat(sep=" ", timespec="milliseconds")
    last = end.isoformat(sep=" ", timespec="milliseconds")
    day = start.date()
    lines = deque(maxlen=800)
    while day <= end.date():
        try:
            with (daily_logs / f"{day.isoformat()}.log").open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if first <= line[:23] <= last:
                        if len(lines) == lines.maxlen or len(line) > 4096:
                            result["truncated"] = True
                        lines.append(line[:4096].rstrip("\r\n"))
        except FileNotFoundError:
            pass
        day += timedelta(days=1)
    result["text"] = "\n".join(lines)
    return result


def launch(source: str, *, env: dict, output=subprocess.DEVNULL,
           scheduled_at: int | None = None, schedule_kind: str | None = None) -> subprocess.Popen | None:
    requested_at = time.time()
    data = Path(env["REWARDS_DATA_DIR"])
    command = [sys.executable, str(Path(__file__).resolve()), source]
    if scheduled_at is not None and schedule_kind is not None:
        command += ["--scheduled-at", str(scheduled_at), "--schedule-kind", schedule_kind]
    proc = subprocess.Popen(
        command,
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
    login: dict[str, int | None] = {}
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
                        login[row["email"].casefold()] = event.get("at")
                    elif kind == "login_ok":
                        login.pop(row["email"].casefold(), None)
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
            if login[key] is not None:
                row["loginRequiredAt"] = login[key]
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
        finished_at = time.time()
        _store_login_failures(conn, _login_failures(accounts, finished_at))
        for account in accounts:
            account.pop("loginRequiredAt", None)
        conn.execute(
            """UPDATE runs SET status = ?, finished_at = ?, accounts = ?, points = ?, exit_code = ?,
               reason = ? WHERE id = ?""",
            (status, finished_at, json.dumps(accounts, ensure_ascii=False), points, code, reason, run_id),
        )
    return status


def _notify(data: Path, run_id: int, source: str, status: str, accounts: list[dict]) -> None:
    if status not in {"failed", "partial"}:
        return
    login_count = sum(account["status"] == "needs_login" for account in accounts)
    source_label = {"manual": "手动任务", "daily": "定时任务", "boot": "开机任务"}[source]
    title = "Rewards 需要重新登录" if login_count else "Rewards 运行失败"
    result = f"{login_count} 个账号需要重新登录" if login_count else ("部分账号未完成" if status == "partial" else "运行未完成")
    try:
        windows_notifications.show(ROOT, data, run_id, title, f"{source_label}：{result}。点击查看本次运行记录。")
    except Exception as exc:  # The result is already committed, independently of Windows delivery.
        try:
            with (data / "notifications.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {exc}\n")
        except OSError:
            pass


def _capture_output(source, destination) -> None:
    console = getattr(sys.stdout, "buffer", None)
    while chunk := source.read1(64 * 1024):
        if destination is not None:
            try:
                destination.write(chunk)
            except OSError:
                destination = None
        if console is not None:
            try:
                console.write(chunk)
                console.flush()
            except OSError:
                console = None


def valid_pause_deadline(value) -> bool:
    return type(value) in (int, float) and 0 < value < MAX_PAUSE_TIMESTAMP


def schedule_pause_state(settings: dict, now: float) -> tuple[bool, float | None]:
    if settings.get("paused") is not True:
        return False, None
    until = settings.get("pausedUntil")
    if valid_pause_deadline(until):
        return now < until, until
    return True, None


def run(data: Path, source: str, scheduled_at: int | None = None, schedule_kind: str | None = None) -> int:
    if source in ("daily", "boot"):
        settings = _saved_schedule(data)
        paused, deadline = schedule_pause_state(settings, time.time())
        active_after = settings.get("activeAfter")
        past_plan = scheduled_at is not None and (
            (type(active_after) in (int, float) and scheduled_at < active_after)
            or (deadline is not None and scheduled_at < deadline)
        )
        if paused or past_plan:
            return 0 if source == "boot" else BUSY_EXIT
    run_id = _claim(data, source, scheduled_at, schedule_kind)
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
                path = _log_file(data, run_id)
                try:
                    path.parent.mkdir(exist_ok=True)
                    capture = path.open("wb", buffering=0)
                except OSError:
                    capture = None
                try:
                    with subprocess.Popen([str(node), "--require", str(preload), str(entry)],
                                          cwd=ROOT / "app", env=env, creationflags=flags,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
                        with closing(_connect(data)) as conn, conn:
                            conn.execute("UPDATE runs SET engine_pid = ? WHERE id = ?", (proc.pid, run_id))
                            stop = conn.execute("SELECT stop_requested FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
                        if stop:
                            _stop_engine(proc.pid)
                        assert proc.stdout is not None
                        _capture_output(proc.stdout, capture)
                        code = proc.wait()
                finally:
                    if capture is not None:
                        capture.close()
                status, accounts, points = _results(Path(temp), code)
                if status in {"failed", "partial"} and not accounts:
                    reason = f"引擎退出，未返回账号结果（退出码 {code}）"
    except OSError:
        reason = "无法启动引擎或读取运行结果"
    status = _finish(data, run_id, status, accounts, points, code, reason)
    _notify(data, run_id, source, status, accounts)
    return code


def _saved_schedule(data: Path) -> dict:
    try:
        settings = json.loads((data / "schedule.json").read_text(encoding="utf-8"))
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=(*SOURCES, "auto"))
    parser.add_argument("--scheduled-at", type=int)
    parser.add_argument("--schedule-kind", choices=("on_time", "catch_up", "retry"))
    options = parser.parse_args()
    if (options.scheduled_at is None) != (options.schedule_kind is None):
        parser.error("--scheduled-at and --schedule-kind must be supplied together")
    data = Path(os.environ.get("REWARDS_DATA_DIR") or ROOT / "data").resolve()
    source = options.source
    if source == "auto":
        source = "boot" if _saved_schedule(data).get("mode") == "boot" else "manual"
    raise SystemExit(run(data, source, options.scheduled_at, options.schedule_kind))
