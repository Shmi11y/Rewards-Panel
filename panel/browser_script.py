"""Keep browser-script settings and serialize starts from pages and the scheduler."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

BRIDGE_PATH = "/api/browser-script/bridge"
BING_HOME = "https://www.bing.com/"
TICKET_SECONDS = 120
HOLD_REASONS = {
    "needs_login": "微软登录已失效或尚未登录",
    "verification_required": "微软要求完成验证",
    "earning_limited": "微软提示计分受限",
    "account_restricted": "微软提示账号异常或受限",
}


def _connect(data: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(data / "browser-script.sqlite3", timeout=5)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            token TEXT NOT NULL,
            open_page INTEGER NOT NULL DEFAULT 0,
            scheduled INTEGER NOT NULL DEFAULT 0,
            last_start REAL,
            ticket TEXT,
            ticket_until REAL,
            held TEXT,
            active_run INTEGER,
            last_seen REAL,
            last_page TEXT,
            script_version TEXT
        )""")
        if conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone() is None:
            conn.execute("INSERT OR IGNORE INTO settings (id, token) VALUES (1, ?)", (secrets.token_urlsafe(32),))
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
        for name, definition in {
            "active_run": "INTEGER", "last_seen": "REAL", "last_page": "TEXT", "script_version": "TEXT",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE settings ADD COLUMN {name} {definition}")
    return conn


def _settings(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()


def _public(row: sqlite3.Row) -> dict:
    last_seen = row["last_seen"]
    connected = type(last_seen) in (int, float) and time.time() - last_seen <= 150
    return {"openPage": bool(row["open_page"]), "scheduled": bool(row["scheduled"]),
            "hold": HOLD_REASONS.get(row["held"], ""),
            "unanswered": bool(row["ticket"] and row["ticket_until"] <= time.time()),
            "connected": connected, "lastSeen": last_seen, "lastPage": row["last_page"] or "",
            "scriptVersion": row["script_version"] or "", "activeRun": row["active_run"]}


def state(data: Path) -> dict:
    with closing(_connect(data)) as conn:
        return _public(_settings(conn))


def token(data: Path) -> str:
    with closing(_connect(data)) as conn:
        return _settings(conn)["token"]


def configure(data: Path, open_page: bool, scheduled: bool) -> dict:
    with closing(_connect(data)) as conn, conn:
        conn.execute("""UPDATE settings SET open_page = ?, scheduled = ?,
                     ticket = CASE WHEN ? THEN ticket END,
                     ticket_until = CASE WHEN ? THEN ticket_until END WHERE id = 1""",
                     (open_page, scheduled, scheduled, scheduled))
        return _public(_settings(conn))


def heartbeat(data: Path, version: str, page: str) -> dict:
    version = version.strip()[:32] if isinstance(version, str) else ""
    page = page.strip()[:120] if isinstance(page, str) else ""
    with closing(_connect(data)) as conn, conn:
        conn.execute("""UPDATE settings SET last_seen = ?, last_page = ?, script_version = ?
                        WHERE id = 1""", (time.time(), page, version))
        return _public(_settings(conn))


def attach_run(data: Path, run_id: int) -> None:
    with closing(_connect(data)) as conn, conn:
        conn.execute("UPDATE settings SET active_run = ? WHERE id = 1", (run_id,))


def active_run(data: Path) -> int | None:
    with closing(_connect(data)) as conn:
        value = _settings(conn)["active_run"]
        return int(value) if isinstance(value, int) else None


def clear_run(data: Path, run_id: int) -> None:
    with closing(_connect(data)) as conn, conn:
        conn.execute("UPDATE settings SET active_run = NULL WHERE id = 1 AND active_run = ?", (run_id,))


def reserve(data: Path) -> tuple[str, str]:
    """Reserve a live schedule ticket before launching the browser."""
    with closing(_connect(data)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _settings(conn)
        if not row["scheduled"]:
            return "", "未开启油猴定时启动"
        if row["held"]:
            return "", HOLD_REASONS[row["held"]] + "，油猴脚本已暂停"
        now = time.time()
        if row["last_start"] is not None and now - row["last_start"] < TICKET_SECONDS:
            return "", "油猴任务刚刚已启动"
        if row["ticket"] and row["ticket_until"] > now:
            return "", "已有油猴定时启动请求"
        ticket = secrets.token_urlsafe(18)
        conn.execute("UPDATE settings SET ticket = ?, ticket_until = ? WHERE id = 1",
                     (ticket, now + TICKET_SECONDS))
        return ticket, ""


def cancel(data: Path, ticket: str) -> None:
    with closing(_connect(data)) as conn, conn:
        conn.execute("UPDATE settings SET ticket = NULL, ticket_until = NULL WHERE id = 1 AND ticket = ?", (ticket,))


def claim(data: Path, ticket: str | None) -> bool:
    """Coalesce page opens and scheduler launches across browser profiles."""
    with closing(_connect(data)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _settings(conn)
        now = time.time()
        if row["held"] or (row["last_start"] is not None and now - row["last_start"] < TICKET_SECONDS):
            return False
        reserved = bool(row["ticket"] and row["ticket_until"] > now)
        if ticket is not None:
            if not (row["scheduled"] and reserved and ticket == row["ticket"]):
                return False
        elif not row["open_page"] or reserved:
            return False
        conn.execute("UPDATE settings SET last_start = ?, ticket = NULL, ticket_until = NULL WHERE id = 1", (now,))
        return True


def hold(data: Path, reason: str) -> bool:
    with closing(_connect(data)) as conn, conn:
        return bool(conn.execute("""UPDATE settings SET held = ?, ticket = NULL, ticket_until = NULL
                                  WHERE id = 1 AND held IS NULL""", (reason,)).rowcount)


def resume(data: Path) -> dict:
    with closing(_connect(data)) as conn, conn:
        conn.execute("UPDATE settings SET held = NULL WHERE id = 1")
        return _public(_settings(conn))


def panel_online(data: Path, port: int) -> bool:
    # This capability only authenticates the browser-script endpoint.
    request = Request(f"http://127.0.0.1:{port}{BRIDGE_PATH}", data=b'{"action":"status"}',
                      headers={"Content-Type": "application/json", "X-Rewards-Script": token(data)})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            result = json.loads(response.read(2048))
        return result.get("ok") is True and result.get("active") is True
    except (OSError, ValueError):
        return False
