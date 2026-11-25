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

import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("REWARDS_DATA_DIR") or ROOT / "data").resolve()
SCHED = DATA / "schedule.json"
PIDFILE = DATA / "scheduler.pid"
SCHEDULER = ROOT / "scheduler.py"
TASK = ROOT / "Run-Task.bat"

STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
BOOT_BAT = STARTUP_DIR / "RewardsBoot.bat"
DAEMON_BAT = STARTUP_DIR / "RewardsScheduler.bat"

DEFAULTS = {"mode": "manual", "times": ["09:10"], "done": {}, "bootDelay": 60,
            "retry": 0, "weekdaysOnly": False}
MAX_TIMES = 4


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


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return bool(ok) and code.value == 259


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


def load() -> dict:
    data = dict(DEFAULTS)
    data["done"] = {}
    data["times"] = list(DEFAULTS["times"])
    if SCHED.exists():
        try:
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
    if not isinstance(data.get("done"), dict):
        data["done"] = {}
    return data


def save(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    SCHED.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def done_today(data: dict) -> list[str]:
    return list((data.get("done") or {}).get(_today()) or [])


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

def next_run_text(data: dict) -> str | None:
    mode = data.get("mode", "manual")
    if mode == "manual":
        return None
    if mode == "boot":
        return "下次开机登录后约 1 分钟"
    now = datetime.now()
    done = set(done_today(data))
    cands = []
    for t in data.get("times", []):
        hh, mm = [int(x) for x in t.split(":")]
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t in done or slot <= now:
            slot += timedelta(days=1)
        cands.append(slot)
    if not cands:
        return None
    return min(cands).strftime("%Y-%m-%d %H:%M")


def status() -> dict:
    data = load()
    st = startup_status()
    st.update({
        "mode": data.get("mode", "manual"),
        "times": data.get("times", []),
        "time": (data.get("times") or ["09:10"])[0],
        "doneToday": done_today(data),
        "retry": data.get("retry", 0),
        "weekdaysOnly": bool(data.get("weekdaysOnly", False)),
        "bootDelay": data.get("bootDelay", 60),
        "nextRun": next_run_text(data),
        "daemonAlive": _pid_alive(daemon_pid()),
        "pythonw": find_pythonw(),
        "taskPath": str(TASK),
    })
    return st


def update(mode: str, times=None, retry=None, weekdays_only=None) -> dict:
    if mode not in ("manual", "daily", "boot"):
        return {"ok": False, "msg": "模式不合法"}
    data = load()
    if times is not None:
        if isinstance(times, str):
            times = [times]
        clean = [t for t in (_norm_time(x) for x in times) if t]
        if not clean:
            return {"ok": False, "msg": "至少保留一个时间点"}
        if len(clean) > MAX_TIMES:
            return {"ok": False, "msg": f"最多 {MAX_TIMES} 个时间点"}
        data["times"] = sorted(set(clean))
    if retry is not None:
        data["retry"] = max(0, min(2, int(retry)))
    if weekdays_only is not None:
        data["weekdaysOnly"] = bool(weekdays_only)
    data["mode"] = mode
    save(data)

    st = apply_startup(mode)
    if mode == "daily":
        ensure_daemon()
    else:
        stop_daemon()
    out = status()
    out["ok"] = True
    out["startup"] = st
    return out


def skip_today() -> dict:
    """把今天所有时间点标记为已完成，今天不再自动触发"""
    data = load()
    day = _today()
    data.setdefault("done", {})[day] = list(data.get("times") or [])
    save(data)
    out = status()
    out["ok"] = True
    return out


def prune_done(data: dict, keep_days: int = 14) -> dict:
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    data["done"] = {k: v for k, v in (data.get("done") or {}).items() if k >= cutoff}
    return data
