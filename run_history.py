"""Persist each task invocation and supervise the engine independently of the panel."""
from __future__ import annotations

import argparse
import ctypes
import gzip
import json
import os
import signal
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import account_holds
import windows_notifications
from panel import browser_script

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
ACCOUNT_FAILURES = {"failed", "needs_login", "skipped", "restricted"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BUSY_EXIT = 75
RUN_LOG_LIMIT = 512 * 1024
NOTIFICATION_LIMIT = 200
MAX_PAUSE_TIMESTAMP = 32_503_680_000
BROWSER_CHANNELS = ("msedge", "chrome", "chromium")


def windows_browser_paths() -> dict[str, str]:
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local = os.environ.get("LOCALAPPDATA")
    locations = {"msedge": ("Microsoft/Edge/Application/msedge.exe", pf86, pf),
                 "chrome": ("Google/Chrome/Application/chrome.exe", pf, pf86)}
    paths = {}
    for channel, (relative, *roots) in locations.items():
        candidates = [root / relative for root in roots]
        if local:
            candidates.append(Path(local) / relative)
        paths[channel] = next((str(path) for path in candidates if path.is_file()), "")
    return paths


def windows_default_browser_channel() -> str:
    if os.name != "nt":
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as key:
            progid = str(winreg.QueryValueEx(key, "ProgId")[0]).lower()
    except OSError:
        return ""
    return {"msedgehtm": "msedge", "chromehtml": "chrome"}.get(progid, "")


def configured_browser_channel(data: Path) -> str:
    try:
        value = json.loads((data / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    channel = value.get("browserChannel") if isinstance(value, dict) else None
    return channel if channel in BROWSER_CHANNELS else ""


def engine_browser_channel(data: Path) -> str:
    channel = configured_browser_channel(data)
    if channel or os.name != "nt":
        return channel or "chromium"
    paths = windows_browser_paths()
    default = windows_default_browser_channel()
    if paths.get(default):
        return default
    return next((name for name, path in paths.items() if path), "chromium")

def _log_file(data: Path, run_id: int) -> Path:
    return data / "run-logs" / f"{run_id}.log"


NOTICE_TYPES = {"success", "failed", "login", "update", "scheduler", "restriction", "reminder"}
PARTICIPATION_MODES = {"reminder", "browser", "automatic"}


def _notices(data: Path) -> sqlite3.Connection:
    data.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data / "notices.sqlite3", timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS notices (
        id INTEGER PRIMARY KEY, event TEXT UNIQUE, category TEXT NOT NULL, runId INTEGER,
        title TEXT NOT NULL, message TEXT NOT NULL, createdAt REAL NOT NULL, read INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS muted (category TEXT PRIMARY KEY)")
    return conn


def record_notice(data: Path, category: str, title: str, message: str, *,
                  run_id: int | None = None, event: str | None = None) -> bool:
    """Return whether Windows delivery is enabled for a newly recorded event."""
    try:
        with closing(_notices(data)) as conn, conn:
            result = conn.execute(
                "INSERT OR IGNORE INTO notices(event, category, runId, title, message, createdAt) VALUES(?,?,?,?,?,?)",
                (event, category, run_id, title, message, time.time()),
            )
            conn.execute("DELETE FROM notices WHERE id NOT IN (SELECT id FROM notices ORDER BY id DESC LIMIT ?)",
                         (NOTIFICATION_LIMIT,))
            return bool(result.rowcount) and conn.execute(
                "SELECT 1 FROM muted WHERE category = ?", (category,),
            ).fetchone() is None
    except (OSError, sqlite3.Error):
        return True


def read_notifications(data: Path) -> dict:
    if not (data / "notices.sqlite3").is_file():
        return {"notifications": [], "muted": [], "unread": 0}
    with closing(_notices(data)) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT id, category, runId, title, message, createdAt, read FROM notices ORDER BY id DESC")]
        muted = [row[0] for row in conn.execute("SELECT category FROM muted")]
    return {"notifications": rows, "muted": muted,
            "unread": sum(not row["read"] and row["category"] not in muted for row in rows)}


def change_notifications(data: Path, action: str, muted=None) -> dict:
    with closing(_notices(data)) as conn, conn:
        if action == "clear":
            conn.execute("DELETE FROM notices")
        elif action == "read":
            conn.execute("UPDATE notices SET read=1")
        elif action == "preferences":
            if not isinstance(muted, list) or any(not isinstance(key, str) or key not in NOTICE_TYPES for key in muted):
                return {"ok": False, "msg": "通知类型不合法"}
            conn.execute("DELETE FROM muted")
            conn.executemany("INSERT OR IGNORE INTO muted VALUES (?)", [(key,) for key in muted])
        else:
            return {"ok": False, "msg": "未知通知操作"}
    return {"ok": True, **read_notifications(data)}


def progress(data: Path, run: dict, history: list[dict]) -> dict:
    rows, _, _, _ = _read_events(data / "run-events" / str(run["id"]))
    accounts = list(rows.values())
    elapsed = max(0, round(time.time() - run["startedAt"]))
    completed = sum(row["status"] not in {"started", "pending"} for row in accounts)
    previous = next((item for item in history if item["id"] != run["id"]
                     and item["status"] == "success" and item["source"] == run["source"]
                     and {row["email"].casefold() for row in item["accounts"]} == set(rows)), None)
    estimate = None
    if previous and previous["finishedAt"]:
        remaining = round(previous["finishedAt"] - previous["startedAt"]) - elapsed
        if remaining > 0:
            estimate = remaining
    return {"runId": run["id"], "accounts": accounts, "completed": completed, "total": len(accounts),
            "elapsedSeconds": elapsed, "remainingSeconds": estimate}


def node_path(root: Path) -> Path | None:
    if os.name == "nt":
        return root / "app" / "node.exe"
    node = shutil.which("node")
    return Path(node) if node else None


def process_alive(pid: int) -> bool:
    if not pid or pid < 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except PermissionError:
            return True
        except ProcessLookupError:
            return False
        except FileNotFoundError:
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


def participation(data: Path) -> str:
    """Return the saved mode, defaulting to reminders until automation is explicitly selected."""
    data.mkdir(parents=True, exist_ok=True)
    with closing(_connect(data)) as conn, conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'participation'").fetchone():
            return conn.execute("SELECT mode FROM participation WHERE id = 1").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS participation (id INTEGER PRIMARY KEY, mode TEXT NOT NULL)")
        legacy = any((data / name).is_file() for name in ("accounts.json", ".env", "schedule.json"))
        if not legacy and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'runs'").fetchone():
            legacy = conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone() is not None
        conn.execute("INSERT OR IGNORE INTO participation VALUES (1, ?)", ("pending" if legacy else "reminder",))
        conn.execute(SCHEMA)
        return conn.execute("SELECT mode FROM participation WHERE id = 1").fetchone()[0]


def set_participation(data: Path, mode) -> dict:
    if not isinstance(mode, str) or mode not in PARTICIPATION_MODES:
        return {"ok": False, "msg": "参与模式不合法"}
    participation(data)
    with closing(_connect(data)) as conn, conn:
        _begin_write(conn)
        active = conn.execute("SELECT runner_pid FROM runs WHERE status = 'running'").fetchone()
        if active and process_alive(active[0]):
            return {"ok": False, "msg": "请等待当前任务结束，或先停止任务"}
        conn.execute("UPDATE participation SET mode = ? WHERE id = 1", (mode,))
    return {"ok": True, "participation": mode}


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


def scheduled_run_finished(data: Path, slot: int) -> bool:
    if not (data / "runs.sqlite3").is_file():
        return False
    with closing(_connect(data)) as conn:
        if not any(row["name"] == "scheduled_at" for row in conn.execute("PRAGMA table_info(runs)")):
            return False
        return conn.execute(
            "SELECT 1 FROM runs WHERE scheduled_at = ? AND status IN ('success', 'skipped', 'reminded')",
            (slot,),
        ).fetchone() is not None


def record_browser_assist(data: Path, *, source: str = "manual") -> int | None:
    run_id = _claim(data, source, None, "browser_assist")
    if run_id is None:
        return None
    _finish(data, run_id, "assisted", [], None, 0, "已从面板打开 Rewards。活动结果可在微软页面核对。")
    record_notice(data, "reminder", "浏览器辅助已打开",
                  "已从面板打开 Rewards，可在微软页面核对活动结果。",
                  run_id=run_id, event=f"browser-assist:{run_id}")
    return run_id


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


def _points_outcome(status: str, points: int | None, accounts: list[dict]) -> str | None:
    if points is not None and points != 0:
        return "increased" if points > 0 else "decreased"
    if points == 0 and any(account.get("points") not in (None, 0) for account in accounts):
        return "net_zero"
    if status != "success":
        return None
    if points is None or any(account["status"] == "success" and account.get("points") is None for account in accounts):
        return "unavailable"
    return "unchanged"


def _view(row: sqlite3.Row) -> dict:
    status = row["status"]
    if status == "running" and not process_alive(row["runner_pid"]):
        status = "interrupted"
    fields = row.keys()
    accounts = json.loads(row["accounts"])
    points = row["points"]
    return {
        "id": row["id"], "source": row["source"], "startedAt": row["started_at"],
        "finishedAt": row["finished_at"], "status": status, "exitCode": row["exit_code"],
        "accounts": accounts, "points": points, "pointsOutcome": _points_outcome(status, points, accounts),
        "reason": row["reason"],
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
            with open_daily_log(daily_logs / f"{day.isoformat()}.log") as stream:
                for line in stream:
                    if first <= line[:23] <= last:
                        if len(lines) == lines.maxlen or len(line) > 4096:
                            result["truncated"] = True
                        lines.append(line[:4096].rstrip("\r\n"))
        except (OSError, EOFError):
            pass
        day += timedelta(days=1)
    result["text"] = "\n".join(lines)
    return result


def open_daily_log(path: Path):
    try:
        return path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return gzip.open(path.with_suffix(".log.gz"), "rt", encoding="utf-8", errors="replace")


def maintain_daily_logs(folder: Path, keep_days: int = 30) -> dict:
    today = datetime.now().date()
    cutoff = today - timedelta(days=keep_days)
    removed = freed = compressed = 0
    for path in folder.iterdir() if folder.is_dir() else []:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.log(?:\.gz)?", path.name)
        if match is None:
            continue
        temporary = None
        try:
            day = datetime.strptime(match[1], "%Y-%m-%d").date()
            info = path.stat()
            size = info.st_size
            if day < cutoff:
                path.unlink()
                removed += 1
                freed += size
            elif day < today and path.suffix == ".log":
                archive = path.with_suffix(".log.gz")
                if archive.exists():
                    continue
                with NamedTemporaryFile(dir=folder, delete=False) as target:
                    temporary = Path(target.name)
                    with path.open("rb") as stream, gzip.GzipFile(fileobj=target, mode="wb") as output:
                        shutil.copyfileobj(stream, output)
                os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns))
                temporary.replace(archive)
                path.unlink()
                compressed += 1
                freed += max(0, size - archive.stat().st_size)
        except (OSError, ValueError):
            pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"removed": removed, "freed": freed, "compressed": compressed}


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


def stop_engine(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.killpg(pid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return not process_alive(pid)
        except OSError:
            return False
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
    if row["engine_pid"] and not stop_engine(row["engine_pid"]):
        with closing(_connect(data)) as conn, conn:
            conn.execute("UPDATE runs SET stop_requested = 0 WHERE id = ? AND status = 'running'", (row["id"],))
        return {"ok": False, "msg": "结束进程失败"}
    return {"ok": True, "stopped": row["engine_pid"] or row["runner_pid"]}


def _read_events(events: Path) -> tuple[dict, dict, tuple[bool, bool], bool]:
    accounts: dict[str, dict] = {}
    login: dict[str, int | None] = {}
    ended = failed = skipped = False

    def account(email: str) -> dict:
        return accounts.setdefault(email.casefold(), {"email": email, "status": "pending", "points": None})

    for path in events.glob("*.jsonl"):
        try:
            stream = path.open(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with stream:
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
                    if kind == "restricted":
                        row["status"] = "restricted"
                        row["restriction"] = event["reason"]
                        row.pop("phase", None)
                    elif row["status"] in {"restricted", "held"}:
                        continue
                    elif kind == "login":
                        login[row["email"].casefold()] = event.get("at")
                    elif kind == "login_ok":
                        login.pop(row["email"].casefold(), None)
                    elif kind == "started":
                        row["status"] = "started"
                    elif kind == "phase":
                        row["phase"] = event["phase"]
                    else:
                        row["status"] = "failed" if kind == "partial" else kind
                        if event.get("points") is not None:
                            row["points"] = event["points"]
                        if type(event.get("balance")) is int:
                            row["balance"] = event["balance"]

    return accounts, login, (ended, failed), skipped


def _results(events: Path, code: int) -> tuple[str, list[dict], int | None]:
    accounts, login, (ended, failed), skipped = _read_events(events)
    for key, row in accounts.items():
        row.pop("phase", None)
        if row["status"] in {"restricted", "held"}:
            continue
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
    elif ended and "success" in statuses and statuses <= {"success", "held"}:
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
                if account["status"] in {"unknown", "interrupted", "not_run"}:
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


def _notify(data: Path, run_id: int, source: str, status: str, accounts: list[dict],
            points: int | None = None) -> None:
    if status not in {"success", "failed", "partial"}:
        return
    if any(row["status"] == "restricted" for row in accounts) and not any(
        row["status"] in {"failed", "needs_login"} for row in accounts
    ):
        return  # The restriction already has its own actionable notice.
    login_count = sum(account["status"] == "needs_login" for account in accounts)
    source_label = {"manual": "手动任务", "daily": "定时任务", "boot": "开机任务"}[source]
    if status == "success":
        title = "Rewards 任务完成"
        result = "任务已完成"
        outcome = _points_outcome(status, points, accounts)
        if outcome == "increased":
            result += f"，积分净增加 {points}"
        elif outcome == "decreased":
            result += f"，积分净减少 {abs(points)}"
        elif outcome == "net_zero":
            result += "，积分净变化为 0"
        elif outcome == "unchanged":
            result += "，本次未观察到积分增加"
        elif outcome == "unavailable":
            result += "，未取得完整计分结果"
    else:
        title = "Rewards 需要重新登录" if login_count else "Rewards 运行失败"
        result = f"{login_count} 个账号需要重新登录" if login_count else ("部分账号未完成" if status == "partial" else "运行未完成")
    message = f"{source_label}：{result}。点击查看本次运行记录。"
    category = "login" if login_count else "success" if status == "success" else "failed"
    _send_notice(data, run_id, category, title, message, f"run:{run_id}")


def _send_notice(data: Path, run_id: int | None, category: str, title: str, message: str, event: str) -> None:
    if not record_notice(data, category, title, message, run_id=run_id, event=event):
        return
    if os.name != "nt":
        return
    try:
        if category == "reminder":
            windows_notifications.show(ROOT, data, None, title, message, reminder=True)
        else:
            windows_notifications.show(ROOT, data, run_id, title, message)
    except Exception as exc:  # The result is already committed, independently of Windows delivery.
        try:
            with (data / "notifications.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {exc}\n")
        except OSError:
            pass


def _watch_account_holds(data: Path, run_id: int, done: threading.Event) -> None:
    sent = set()
    while True:
        final_scan = done.is_set()
        for path in (data / "account-holds").glob("*.json"):
            if path in sent:
                continue
            hold = account_holds.read_file(path)
            if hold and hold.get("runId") == run_id and hold["reason"] in account_holds.REASONS:
                sent.add(path)
                message = account_holds.REASONS[hold["reason"]] + "。已暂停该账号，请到微软官网确认后手动恢复。点击查看运行记录。"
                _send_notice(data, run_id, "restriction", "Rewards 已暂停异常账号", message,
                             f"restriction:{run_id}:{path.stem}")
        if final_scan:
            return
        done.wait(0.4)


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


def notify_browser_script_hold(data: Path, reason: str) -> None:
    category = "login" if reason == "needs_login" else "restriction"
    _send_notice(data, None, category, "Rewards 助手已暂停",
                 browser_script.HOLD_REASONS[reason] + "。请在微软官网确认后恢复。", f"browser-hold:{time.time_ns()}")


def _run_browser_schedule(data: Path, run_id: int) -> None:
    ticket, reason = browser_script.reserve(data)
    if not ticket:
        _finish(data, run_id, "skipped", [], None, 0, reason)
        return
    channel = configured_browser_channel(data) or windows_default_browser_channel()
    executable = windows_browser_paths().get(channel)
    if not executable:
        reason = "油猴定时需要在「参与模式」选择已安装 Tampermonkey 的 Edge 或 Chrome"
    elif not browser_script.panel_online(data, int(os.environ.get("REWARDS_PORT", "8765"))):
        reason = "桌面面板未运行，请保持打开或最小化到托盘"
    else:
        try:
            subprocess.Popen([executable, f"{browser_script.BING_HOME}#rp-start={ticket}"], creationflags=NO_WINDOW)
        except OSError:
            reason = "无法打开所选浏览器"
        else:
            _finish(data, run_id, "assisted", [], None, 0, "已打开浏览器，等待 Rewards 助手接手")
            return
    browser_script.cancel(data, ticket)
    _finish(data, run_id, "skipped", [], None, 0, reason)
    _send_notice(data, run_id, "reminder", "油猴定时未启动", reason, f"browser-schedule:{run_id}")


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
    participation(data)
    run_id = _claim(data, source, scheduled_at, schedule_kind)
    if run_id is None:
        return BUSY_EXIT
    mode = participation(data)
    if (os.name == "nt" and mode == "browser" and source in ("daily", "boot")
            and schedule_kind != "retry" and browser_script.state(data)["scheduled"]):
        _run_browser_schedule(data, run_id)
        return 0
    if mode != "automatic":
        if mode == "pending" or schedule_kind == "retry":
            reason = "请在面板确认参与模式" if mode == "pending" else "仅查看与提醒模式已跳过任务重试"
            _finish(data, run_id, "skipped", [], None, 0, reason)
        else:
            label = {"manual": "手动提醒", "daily": "每日提醒", "boot": "开机提醒"}[source]
            if schedule_kind == "catch_up":
                label = "定时补提醒"
            _finish(data, run_id, "reminded", [], None, 0, None)
            _send_notice(data, run_id, "reminder", "Microsoft Rewards 提醒",
                         f"{label}。点击打开微软 Rewards。", f"reminder:{run_id}")
        return 0
    code, reason = 1, None
    status, accounts, points = "failed", [], None
    try:
        (data / "task.pid").write_text(str(os.getpid()), encoding="utf-8")
        maintain_daily_logs(ROOT / "app" / "logs")
        events = data / "run-events" / str(run_id)
        events.mkdir(parents=True, exist_ok=True)
        try:
            env = dict(os.environ, REWARDS_DATA_DIR=str(data), REWARDS_APP_ROOT=str(ROOT),
                       PLAYWRIGHT_BROWSERS_PATH=str(ROOT / "browser"), REWARDS_RUN_EVENTS=str(events),
                       NODE_NO_WARNINGS="1", REWARDS_RUN_ID=str(run_id))
            if os.name == "nt" and not env.get("CONFIG_BROWSER_CHANNEL"):
                env["CONFIG_BROWSER_CHANNEL"] = engine_browser_channel(data)
            node, entry, preload = node_path(ROOT), ROOT / "app/dist/index.js", ROOT / "task-data.cjs"
            missing = next((path for path in (entry, preload) if not path.is_file()), None)
            if node is None or not node.is_file():
                reason = "缺少 Node.js"
            elif missing:
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
                                          start_new_session=os.name != "nt",
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
                        with closing(_connect(data)) as conn, conn:
                            conn.execute("UPDATE runs SET engine_pid = ? WHERE id = ?", (proc.pid, run_id))
                            stop = conn.execute("SELECT stop_requested FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
                        if stop:
                            stop_engine(proc.pid)
                        assert proc.stdout is not None
                        done = threading.Event()
                        watcher = threading.Thread(target=_watch_account_holds, args=(data, run_id, done), daemon=True)
                        watcher.start()
                        try:
                            _capture_output(proc.stdout, capture)
                            code = proc.wait()
                        finally:
                            done.set()
                            watcher.join()
                finally:
                    if capture is not None:
                        capture.close()
                status, accounts, points = _results(events, code)
                if status in {"failed", "partial"} and not accounts:
                    reason = f"引擎退出，未返回账号结果（退出码 {code}）"
        finally:
            for event_file in events.glob("*.jsonl"):
                event_file.unlink(missing_ok=True)
            events.rmdir()
    except OSError:
        reason = "无法启动引擎或读取运行结果"
    status = _finish(data, run_id, status, accounts, points, code, reason)
    try:
        from panel import balance
        for account in accounts:
            if "balance" in account:
                balance.save(data, account["email"], account["balance"], time.time(), "log")
    except (OSError, sqlite3.Error):
        pass
    _notify(data, run_id, source, status, accounts, points)
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
