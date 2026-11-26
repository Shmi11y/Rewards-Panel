#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时设置：模式、多时间点、跳过今天、失败重试、开机启动项、守护进程

schedule.json:
    {
      "mode": "manual|daily|boot",
      "times": ["09:10", "21:30"],
      "done":  {"2026-11-24": ["09:10"]},
      "bootDelay": 60,
      "retry": 1
    }

开机自启走「启动」文件夹，不写注册表也不需要管理员。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parent.parent
import run_history

DATA = Path(os.environ.get("REWARDS_DATA_DIR") or ROOT / "data").resolve()
SCHED = DATA / "schedule.json"
PIDFILE = DATA / "scheduler.pid"
SUPERVISOR_PIDFILE = DATA / "scheduler-supervisor.pid"
SCHEDULER = ROOT / "scheduler.py"
TASK = ROOT / "Run-Task.bat"

STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
BOOT_BAT = STARTUP_DIR / "RewardsBoot.bat"
DAEMON_BAT = STARTUP_DIR / "RewardsScheduler.bat"

DEFAULTS = {"mode": "manual", "times": ["09:10"], "done": {}, "bootDelay": 60,
            "retry": 0, "weekdaysOnly": False, "catchUpMode": "latest", "catchUpMinutes": 720,
            "paused": False}
MAX_TIMES = 4
MAX_CATCH_UP_MINUTES = 1440


# ---------------------------------------------------------------- 基础

def find_pythonw() -> str | None:
    cands = [
        ROOT / "runtime" / "python" / "pythonw.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python313" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ]
    for c in cands:
        if c.exists():
            return str(c)
    return None


_pid_alive = run_history.process_alive


def daemon_pid() -> int:
    try:
        return int(PIDFILE.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------- 存取

def _norm_time(t: str) -> str | None:
    try:
        hh, mm = [int(x) for x in str(t).strip().split(":")]
        if 0 <= hh < 24 and 0 <= mm < 60:
            return f"{hh:02d}:{mm:02d}"
    except Exception:
        pass
    return None


def load(*, now: float | None = None) -> dict:
    data = dict(DEFAULTS)
    data["done"] = {}
    data["times"] = list(DEFAULTS["times"])
    last_modified = 0
    if SCHED.exists():
        try:
            last_modified = SCHED.stat().st_mtime
            got = json.loads(SCHED.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                if "time" in got and "times" not in got:      # 兼容旧格式
                    got["times"] = [got.pop("time")]
                if not isinstance(got.get("times"), list):
                    got["times"] = list(DEFAULTS["times"])
                data.update(got)
        except Exception:
            pass
    times = [t for t in (_norm_time(x) for x in data.get("times") or []) if t]
    data["times"] = sorted(set(times))[:MAX_TIMES] or list(DEFAULTS["times"])
    groups = data.get("groups")
    data["groups"] = {slot: group for slot, group in groups.items()
                      if slot in data["times"] and valid_group(group)} if isinstance(groups, dict) else {}
    if not isinstance(data.get("done"), dict):
        data["done"] = {}
    if type(data.get("activeAfter")) not in (int, float) or not 0 <= data["activeAfter"] < float("inf"):
        data["activeAfter"] = last_modified
    if data["catchUpMode"] not in ("latest", "skip"):
        data["catchUpMode"] = DEFAULTS["catchUpMode"]
    if type(data["catchUpMinutes"]) is not int or not 1 <= data["catchUpMinutes"] <= MAX_CATCH_UP_MINUTES:
        data["catchUpMinutes"] = DEFAULTS["catchUpMinutes"]
    paused, deadline = run_history.schedule_pause_state(data, time.time() if now is None else now)
    data["paused"] = paused
    if paused and deadline is not None:
        data["pausedUntil"] = deadline
    else:
        data.pop("pausedUntil", None)
        if deadline is not None:
            data["activeAfter"] = max(data["activeAfter"], deadline)
    return data


def save(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    stream = NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA, delete=False)
    staging = Path(stream.name)
    try:
        with stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        staging.replace(SCHED)
    finally:
        staging.unlink(missing_ok=True)


def slot_timestamp(day: date, slot: str) -> int:
    hour, minute = map(int, slot.split(":"))
    return int(datetime(day.year, day.month, day.day, hour, minute).timestamp())


def done_today(data: dict, now: datetime) -> list[str]:
    day = now.date()
    legacy = data["done"].get(day.isoformat()) or []
    if data.get("skipDate") == day.isoformat():
        return list(data["times"])
    try:
        handled = run_history.processed_slots(DATA, slot_timestamp(day, "00:00"))
    except sqlite3.Error:
        handled = set()
    return [slot for slot in data["times"] if slot in legacy or slot_timestamp(day, slot) in handled]


# ---------------------------------------------------------------- 启动项

def _write_bat(path: Path, lines: list[str]) -> None:
    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    body = "@echo off\r\n" + "\r\n".join(lines) + "\r\n"
    path.write_bytes(body.encode("gbk", "replace"))


def apply_startup(mode: str) -> dict:
    for f in (BOOT_BAT, DAEMON_BAT):
        try:
            if f.exists():
                f.unlink()
        except Exception:
            pass

    if mode == "boot":
        _write_bat(BOOT_BAT, [
            "rem auto start on logon: wait then run task silently",
            f'set "REWARDS_DATA_DIR={DATA}"',
            "ping -n 61 127.0.0.1 >nul",
            f'cd /d "{ROOT}"',
            f'start "" /min cmd /c ""{TASK}" /silent /boot"',
        ])
    elif mode == "daily":
        pyw = find_pythonw()
        if pyw:
            _write_bat(DAEMON_BAT, [
                "rem keep scheduler daemon alive",
                f'set "REWARDS_DATA_DIR={DATA}"',
                f'start "" /min "{pyw}" "{SCHEDULER}"',
            ])
    return startup_status()


def startup_status() -> dict:
    return {"boot": BOOT_BAT.exists(), "daily": DAEMON_BAT.exists(),
            "startupDir": str(STARTUP_DIR)}


# ---------------------------------------------------------------- 守护

def ensure_daemon() -> bool:
    if _pid_alive(daemon_pid()):
        return True
    pyw = find_pythonw()
    if not pyw:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    env = dict(os.environ)
    env["REWARDS_DATA_DIR"] = str(DATA)
    subprocess.Popen([pyw, str(SCHEDULER)], cwd=str(ROOT), env=env,
                     creationflags=flags, close_fds=True)
    for _ in range(16):
        if _pid_alive(daemon_pid()):
            return True
        time.sleep(0.15)
    return False


def supervise_if_missing() -> None:
    try:
        supervisor = int(SUPERVISOR_PIDFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        supervisor = 0
    if load()["mode"] == "daily" and not _pid_alive(supervisor):
        ensure_daemon()


def stop_daemon() -> bool:
    pid = daemon_pid()
    if not _pid_alive(pid):
        return False
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=8,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 计算

def next_run_text(data: dict, now: datetime, done: list[str]) -> str | None:
    mode = data.get("mode", "manual")
    if mode == "manual" or data.get("paused"):
        return None
    if mode == "boot":
        return "下次开机登录后约 1 分钟"
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if data["weekdaysOnly"] and day.weekday() >= 5:
            continue
        for slot in data["times"]:
            due = slot_timestamp(day, slot)
            if due <= now.timestamp() or (offset == 0 and slot in done):
                continue
            return datetime.fromtimestamp(due).strftime("%Y-%m-%d %H:%M")
    return None


def status() -> dict:
    now = datetime.now()
    data = load(now=now.timestamp())
    done = done_today(data, now)
    st = startup_status()
    st.update({
        "mode": data.get("mode", "manual"),
        "times": data.get("times", []),
        "groups": data["groups"],
        "time": (data.get("times") or ["09:10"])[0],
        "doneToday": done,
        "retry": data.get("retry", 0),
        "weekdaysOnly": bool(data.get("weekdaysOnly", False)),
        "bootDelay": data.get("bootDelay", 60),
        "catchUpMode": data["catchUpMode"],
        "catchUpMinutes": data["catchUpMinutes"],
        "paused": data["paused"],
        "pauseUntil": (
            datetime.fromtimestamp(data["pausedUntil"]).strftime("%Y-%m-%d %H:%M")
            if "pausedUntil" in data else None
        ),
        "nextRun": next_run_text(data, now, done),
        "daemonAlive": _pid_alive(daemon_pid()),
        "pythonw": find_pythonw(),
        "taskPath": str(TASK),
    })
    return st


def valid_group(group) -> bool:
    return isinstance(group, str) and 0 < len(group) <= 32 and group == group.strip() and group.isprintable()


def update(mode: str, times=None, retry=None, weekdays_only=None, catch_up_mode=None, catch_up_minutes=None,
           groups=None) -> dict:
    if mode not in ("manual", "daily", "boot"):
        return {"ok": False, "msg": "模式不合法"}
    data = load()
    previous_plan = (data["mode"], data["times"], data["weekdaysOnly"], data["groups"])
    if times is not None:
        if isinstance(times, str):
            times = [times]
        clean = [t for t in (_norm_time(x) for x in times) if t]
        if not clean:
            return {"ok": False, "msg": "至少保留一个时间点"}
        if len(clean) > MAX_TIMES:
            return {"ok": False, "msg": f"最多 {MAX_TIMES} 个时间点"}
        data["times"] = sorted(set(clean))
    if groups is not None:
        if not isinstance(groups, dict) or any(slot not in data["times"] or not valid_group(group)
                                               for slot, group in groups.items()):
            return {"ok": False, "msg": "定时分组不合法"}
        data["groups"] = groups
    else:
        data["groups"] = {slot: group for slot, group in data["groups"].items() if slot in data["times"]}
    if retry is not None:
        data["retry"] = max(0, min(2, int(retry)))
    if weekdays_only is not None:
        data["weekdaysOnly"] = bool(weekdays_only)
    if catch_up_mode is not None:
        if catch_up_mode not in ("latest", "skip"):
            return {"ok": False, "msg": "补跑规则不合法"}
        data["catchUpMode"] = catch_up_mode
    if catch_up_minutes is not None:
        if type(catch_up_minutes) is not int or not 1 <= catch_up_minutes <= MAX_CATCH_UP_MINUTES:
            return {"ok": False, "msg": f"补跑时限须为 1 到 {MAX_CATCH_UP_MINUTES} 分钟"}
        data["catchUpMinutes"] = catch_up_minutes
    data["mode"] = mode
    if mode != previous_plan[0]:
        data["paused"] = False
        data.pop("pausedUntil", None)
    if (mode, data["times"], data["weekdaysOnly"], data["groups"]) != previous_plan:
        data["activeAfter"] = time.time()
    save(data)

    startup = sync_automation(mode)
    return {**status(), "ok": True, "startup": startup}


def sync_automation(mode: str) -> dict:
    startup = apply_startup(mode)
    if mode == "daily":
        ensure_daemon()
    else:
        stop_daemon()
    return startup


def set_paused(paused: bool, until=None) -> dict:
    if type(paused) is not bool or (not paused and until is not None):
        return {"ok": False, "msg": "暂停设置须为布尔值"}
    now = datetime.now()
    deadline = None
    if until is not None:
        try:
            if until == "tomorrow":
                tomorrow = now.date() + timedelta(days=1)
                end = datetime.combine(tomorrow, datetime.min.time())
            else:
                end = datetime.strptime(until, "%Y-%m-%dT%H:%M")
            deadline = end.timestamp()
            if (end <= now or not run_history.valid_pause_deadline(deadline)
                    or datetime.fromtimestamp(deadline) != end):
                raise ValueError
        except (TypeError, ValueError, OverflowError, OSError):
            return {"ok": False, "msg": "请选择有效的未来日期和时间"}
    data = load()
    if data["mode"] not in ("daily", "boot"):
        return {"ok": False, "msg": "尚未启用自动运行"}
    if not paused and data["mode"] == "daily":
        try:
            running = ensure_daemon()
        except OSError:
            running = False
        if not running:
            return {"ok": False, "msg": "定时守护未能启动，请重试"}
    if paused != data["paused"] or deadline != data.get("pausedUntil"):
        data["paused"] = paused
        if deadline is None:
            data.pop("pausedUntil", None)
        else:
            data["pausedUntil"] = deadline
        data["activeAfter"] = max(data["activeAfter"], time.time())
        save(data)
    if deadline is not None and data["mode"] == "daily":
        try:
            ensure_daemon()
        except OSError:
            pass
    return {"ok": True, **status()}


def skip_today() -> dict:
    """把今天所有时间点标记为已完成，今天不再自动触发"""
    data = load()
    now = datetime.now()
    date = now.date()
    day = date.isoformat()
    handled = done_today(data, now)
    data.setdefault("done", {})[day] = list(data.get("times") or [])
    data["skipDate"] = day
    save(data)
    if data["mode"] == "daily" and (not data["weekdaysOnly"] or date.weekday() < 5):
        upcoming = [slot_timestamp(date, slot) for slot in data["times"] if slot not in handled]
        run_history.record_skips(DATA, [(slot, "手动选择今天跳过") for slot in upcoming if slot >= data["activeAfter"]])
    out = status()
    out["ok"] = True
    return out


def prune_done(data: dict, keep_days: int = 14) -> dict:
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    data["done"] = {k: v for k, v in (data.get("done") or {}).items() if k >= cutoff}
    return data
