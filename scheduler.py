#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rewards 定时守护

由开机启动项拉起，无窗口常驻。每 20 秒读一次 data/schedule.json，
所以在面板改设置立即生效，不用重启。

职责：
    * mode=daily  到点触发（支持多个时间点，同一时间点一天只跑一次）
    * mode=boot   由 Startup 里的 RewardsBoot.bat 负责，这里空转
    * 失败重试：触发后若 3 分钟内任务没起来、或日志没有任何新写入，
                 按 schedule.json 的 retry 次数重试（0~2 次）
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import run_history

DATA = Path(os.environ.get("REWARDS_DATA_DIR") or ROOT / "data").resolve()
SCHED = DATA / "schedule.json"
PIDFILE = DATA / "scheduler.pid"
TASK_PID = DATA / "task.pid"
POLL_SEC = 20
RETRY_AFTER = 180          # 触发后多久判定“没起来”
MAX_RETRY = 2


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        (ROOT / "logs").mkdir(exist_ok=True)
        with open(ROOT / "logs" / "scheduler.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


_pid_alive = run_history.process_alive


def read_schedule() -> dict:
    data = {"mode": "manual", "times": ["09:10"], "done": {}, "bootDelay": 60, "retry": 0}
    try:
        got = json.loads(SCHED.read_text(encoding="utf-8"))
        if isinstance(got, dict):
            if "time" in got and "times" not in got:
                got["times"] = [got.pop("time")]
            data.update(got)
    except Exception:
        pass
    if not isinstance(data.get("done"), dict):
        data["done"] = {}
    if not isinstance(data.get("times"), list):
        data["times"] = ["09:10"]
    return data


def write_schedule(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    SCHED.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def task_running() -> bool:
    if _pid_alive(_read_pid(TASK_PID)):
        return True
    logfile = ROOT / "app" / "logs" / f"{datetime.now():%Y-%m-%d}.log"
    try:
        return logfile.exists() and (time.time() - logfile.stat().st_mtime) < 120
    except Exception:
        return False


def log_mtime() -> float:
    logfile = ROOT / "app" / "logs" / f"{datetime.now():%Y-%m-%d}.log"
    try:
        return logfile.stat().st_mtime
    except Exception:
        return 0.0


def trigger(reason: str, *, source: str = "daily") -> int:
    """返回新进程 pid，失败返回 0"""
    if task_running():
        log(f"跳过（已有任务在跑）: {reason}")
        return 0

    env = dict(os.environ, REWARDS_DATA_DIR=str(DATA))
    (ROOT / "logs").mkdir(exist_ok=True)
    try:
        with (ROOT / "logs" / f"task-{datetime.now():%Y-%m-%d}.out").open("ab") as out:
            proc = run_history.launch(source, env=env, output=out)
    except Exception as exc:  # noqa: BLE001
        log(f"启动失败: {exc}")
        return 0
    if proc is None:
        log(f"跳过（已有任务在跑）: {reason}")
        return 0
    log(f"已触发 pid={proc.pid} 原因={reason}")
    return proc.pid


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"守护启动 pid={os.getpid()}")
    booted_at = time.time()
    boot_once_done = False
    pending = None   # {"slot": "09:10", "at": ts, "mtime": float, "tries": int}

    try:
        while True:
            try:
                sched = read_schedule()
                mode = sched.get("mode", "manual")
                today = datetime.now().strftime("%Y-%m-%d")
                done = list((sched.get("done") or {}).get(today) or [])
                retry_left = max(0, min(MAX_RETRY, int(sched.get("retry", 0) or 0)))

                # --- 开机模式 ---
                if mode == "boot" and not boot_once_done and time.time() - booted_at > sched.get("bootDelay", 60):
                    if trigger("开机自动运行", source="boot"):
                        boot_once_done = True

                # --- 每日多时间点 ---
                elif mode == "daily":
                    now = datetime.now()
                    if sched.get("weekdaysOnly") and now.weekday() >= 5:
                        time.sleep(POLL_SEC)
                        continue
                    for slot in sched.get("times") or []:
                        if slot in done:
                            continue
                        try:
                            hh, mm = [int(x) for x in slot.split(":")]
                        except Exception:
                            continue
                        if (now.hour, now.minute) < (hh, mm):
                            continue
                        previous_mtime = log_mtime()
                        pid = trigger(f"每天 {slot} 定时")
                        if pid:
                            done.append(slot)
                            sched.setdefault("done", {})[today] = done
                            write_schedule(sched)
                            pending = {"slot": slot, "at": time.time(),
                                       "mtime": previous_mtime, "tries": 1, "left": retry_left}
                        break

                # --- 重试判定 ---
                if pending:
                    elapsed = time.time() - pending["at"]
                    if elapsed > RETRY_AFTER:
                        moved = log_mtime() > pending["mtime"] + 1
                        alive = task_running()
                        if not moved and not alive and pending["left"] > 0:
                            pending["left"] -= 1
                            pending["tries"] += 1
                            log(f"重试 {pending['tries']} 次：{pending['slot']} 触发后无日志输出")
                            pending["mtime"] = log_mtime()
                            trigger(f"重试 {pending['slot']} 定时")
                            pending["at"] = time.time()
                        else:
                            if not moved and not alive:
                                log(f"放弃重试：{pending['slot']} 无输出且已无重试次数")
                            pending = None

            except Exception as exc:  # 任何异常都不许把守护带走
                log(f"循环异常（已忽略）: {exc}")

            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            PIDFILE.unlink()
        except Exception:
            pass
        log("守护退出")


if __name__ == "__main__":
    sys.exit(main())
