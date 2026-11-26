"""Read Rewards balances without starting the task engine."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from http.client import HTTPSConnection, HTTPException
from pathlib import Path

ENDPOINTS = (
    ("rewards.bing.com", "/api/getuserinfo"),
    ("www.bing.com", "/rewards/panelflyout/getuserinfo?channel=BingFlyout&partnerId=BingRewards"),
)


def recent(data: Path) -> dict:
    database = data / "balances.sqlite3"
    if not database.is_file():
        return {}
    with closing(sqlite3.connect(database, timeout=2)) as conn:
        try:
            rows = conn.execute("SELECT email, points, at, source FROM balances").fetchall()
        except sqlite3.OperationalError:
            return {}
    return {email: {"balance": points, "at": at, "source": source}
            for email, points, at, source in rows}


def save(data: Path, email: str, points: int, at: float, source: str) -> None:
    with closing(sqlite3.connect(data / "balances.sqlite3", timeout=5)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS balances (
            email TEXT PRIMARY KEY, points INTEGER NOT NULL, at REAL NOT NULL, source TEXT NOT NULL
        )""")
        conn.execute("""INSERT INTO balances VALUES (?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET points=excluded.points, at=excluded.at, source=excluded.source
            WHERE excluded.at >= balances.at""", (email.casefold(), points, at, source))


def _cookie_header(state: dict, host: str, route: str) -> str:
    cookies = []
    route = route.split("?", 1)[0]
    now = time.time()
    for cookie in state.get("cookies", []):
        domain = cookie.get("domain", "").lower()
        path = cookie.get("path", "/")
        name, value = cookie.get("name", ""), cookie.get("value", "")
        expiry = cookie.get("expires", -1)
        if (host != domain.lstrip(".") and not (domain.startswith(".") and host.endswith(domain))):
            continue
        if route != path and not route.startswith(path.rstrip("/") + "/"):
            continue
        if (expiry != -1 and expiry <= now) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            continue
        if re.fullmatch(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]*", value):
            cookies.append((len(path), f"{name}={value}"))
    return "; ".join(value for _, value in sorted(cookies, key=lambda item: -item[0]))


def _request(host: str, route: str, cookie: str) -> tuple[int, dict]:
    with closing(HTTPSConnection(host, timeout=5)) as connection:
        connection.request("GET", route, headers={
        "Cookie": cookie, "Accept": "application/json", "Referer": f"https://{host}/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        })
        response = connection.getresponse()
        if response.status != 200:
            return response.status, {}
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            return 200, {}
        try:
            result = json.loads(raw)
        except ValueError:
            return 200, {}
        return 200, result if isinstance(result, dict) else {}


def _points(payload: dict, flyout: bool) -> int | None:
    record = payload.get("flyoutResult" if flyout else "dashboard") or {}
    status = record.get("userStatus") or {}
    if payload.get("isError") or status.get("isRewardsUser") is False:
        return None
    points = status.get("availablePoints")
    if points is None and flyout and status.get("isRewardsUser") is True:
        points = (payload.get("userInfo") or {}).get("balance")
    return points if type(points) is int and 0 <= points <= 2**53 - 1 else None


def read(db: Path, email: str) -> int:
    if not db.is_file():
        raise ValueError("请先登录此账号")
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        states = conn.execute(
            "SELECT storage_state FROM sessions WHERE email = ? COLLATE NOCASE AND storage_state IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1", (email,),
        ).fetchall()
    if not states:
        raise ValueError("请先登录此账号")
    state = json.loads(states[0][0])
    transient = False
    for index, (host, route) in enumerate(ENDPOINTS):
        cookie = _cookie_header(state, host, route)
        if not cookie:
            continue
        for attempt in range(2):
            try:
                code, payload = _request(host, route, cookie)
            except (OSError, HTTPException):
                code, payload = 503, {}
            if code == 200 and (points := _points(payload, index == 1)) is not None:
                return points
            if code < 500:
                break
            transient = True
            if attempt == 0:
                time.sleep(1)
    raise ValueError("网络或微软服务暂不可用，请稍后重试" if transient else "微软未返回余额，请检查登录会话")
