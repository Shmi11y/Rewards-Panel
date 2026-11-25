#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rewards 定时守护

由开机启动项拉起，无窗口常驻。每 20 秒读一次 data/schedule.json，
所以在面板改设置立即生效，不用重启。

职责：
    * mode=daily  到点触发（支持多个时间点，同一时间点一天只跑一次）
    * 守护启动或休眠恢复后按配置补最近一次，或记录跳过
    * mode=boot   由 Startup 里的 RewardsBoot.bat 负责，这里空转
    * 失败重试：触发后若 3 分钟内任务没起来、或日志没有任何新写入，
                 按 schedule.json 的 retry 次数重试（0~2 次）
"""
from __future__ import annotations

import msvcrt
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import run_history
from panel import schedule

DATA = Path(os.environ.get("REWARDS_DATA_DIR") or ROOT / "data").resolve()
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


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def task_running() -> bool:
    pid = _read_pid(TASK_PID)
    if pid:
        return _pid_alive(pid)
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


def trigger(reason: str, *, source: str = "daily", slot: int | None = None, kind: str | None = None) -> int:
    """返回新进程 pid，失败返回 0"""
    if task_running():
        log(f"跳过（已有任务在跑）: {reason}")
        return 0

    env = dict(os.environ, REWARDS_DATA_DIR=str(DATA))
    (ROOT / "logs").mkdir(exist_ok=True)
    try:
        with (ROOT / "logs" / f"task-{datetime.now():%Y-%m-%d}.out").open("ab") as out:
            proc = run_history.launch(source, env=env, output=out, scheduled_at=slot, schedule_kind=kind)
    except Exception as exc:  # noqa: BLE001
        log(f"启动失败: {exc}")
        return 0
    if proc is None:
        log(f"跳过（已有任务在跑）: {reason}")
        return 0
    log(f"已触发 pid={proc.pid} 原因={reason}")
    return proc.pid


def plan_daily(settings: dict, now: datetime, previous_poll: float | None, on_time: set[int],
               handled: set[int]) -> tuple[list[tuple[int, str]], tuple[int, str] | None]:
    """Return the missed slots to record and the single next launch candidate."""
    today = now.date()
    stamp = now.timestamp()
    weekdays_only = settings["weekdaysOnly"]
    due = []
    for days_ago in range(6, -1, -1):
        date = today - timedelta(days=days_ago)
        if weekdays_only and date.weekday() >= 5:
            continue
        previous = settings["done"].get(date.isoformat()) or []
        for name in settings["times"]:
            slot = schedule.slot_timestamp(date, name)
            if settings["activeAfter"] <= slot <= stamp:
                due.append((slot, slot in handled or name in previous))

    unresolved = [slot for slot, processed in due if not processed]
    if previous_poll is None or not 0 <= stamp - previous_poll <= POLL_SEC * 2:
        on_time.clear()
    else:
        on_time.intersection_update(unresolved)
        on_time.update(slot for slot in unresolved if previous_poll < slot)

    skipped = []
    launch = None
    today_skipped = settings.get("skipDate") == today.isoformat()
    outside_workdays = weekdays_only and today.weekday() >= 5
    for slot in unresolved:
        if today_skipped:
            reason = "手动选择今天跳过"
        elif outside_workdays:
            reason = "恢复时为非工作日"
        elif slot in on_time:
            if launch is None:
                launch = (slot, "on_time")
            continue
        elif settings["catchUpMode"] == "skip":
            reason = "补跑规则设为直接跳过"
        elif slot != due[-1][0]:
            reason = "存在更新的定时，跳过较早的时间点"
        elif stamp - slot > settings["catchUpMinutes"] * 60:
            reason = f"超过补跑时限（{settings['catchUpMinutes']} 分钟）"
        else:
            if launch is None:
                launch = (slot, "catch_up")
            continue
        skipped.append((slot, reason))
    return skipped, launch


def _loop() -> None:
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"守护启动 pid={os.getpid()}")
    booted_at = time.time()
    boot_once_done = False
    pending = None
    previous_poll = None
    on_time: set[int] = set()

    try:
        while True:
            try:
                sched = schedule.load()
                mode = sched["mode"]
                now = datetime.now()
                retry_left = max(0, min(MAX_RETRY, int(sched.get("retry", 0) or 0)))

                # --- 开机模式 ---
                if mode == "boot" and not boot_once_done and time.time() - booted_at > sched.get("bootDelay", 60):
                    if trigger("开机自动运行", source="boot"):
                        boot_once_done = True

                # --- 每日多时间点 ---
                elif mode == "daily":
                    earliest = schedule.slot_timestamp(now.date() - timedelta(days=6), "00:00")
                    handled = run_history.processed_slots(DATA, earliest)
                    skipped, next_job = plan_daily(sched, now, previous_poll, on_time, handled)
                    run_history.record_skips(DATA, skipped)
                    if next_job:
                        slot, kind = next_job
                        previous_mtime = log_mtime()
                        planned_time = datetime.fromtimestamp(slot).strftime("%Y-%m-%d %H:%M")
                        label = "补跑" if kind == "catch_up" else "定时"
                        pid = trigger(f"{label} {planned_time}", slot=slot, kind=kind)
                        if pid:
                            on_time.discard(slot)
                            pending = {"slot": slot, "kind": kind, "at": time.time(),
                                       "mtime": previous_mtime, "tries": 1, "left": retry_left}
                if mode != "daily":
                    on_time.clear()
                    pending = None
                previous_poll = now.timestamp()

                # --- 重试判定 ---
                if pending and (pending["slot"] < sched["activeAfter"] or
                                sched.get("skipDate") == now.date().isoformat() or
                                (sched["weekdaysOnly"] and now.weekday() >= 5)):
                    pending = None
                if pending and pending["kind"] == "catch_up" and (
                    sched["catchUpMode"] == "skip" or now.timestamp() - pending["slot"] > sched["catchUpMinutes"] * 60
                ):
                    pending = None
                if pending:
                    elapsed = time.time() - pending["at"]
                    if elapsed > RETRY_AFTER:
                        moved = log_mtime() > pending["mtime"]
                        alive = task_running()
                        if not moved and not alive and pending["left"] > 0:
                            pending["left"] -= 1
                            pending["tries"] += 1
                            log(f"重试 {pending['tries']} 次：{pending['slot']} 触发后无日志输出")
                            pending["mtime"] = log_mtime()
                            trigger("定时重试", slot=pending["slot"], kind="retry")
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


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "scheduler.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        _loop()


if __name__ == "__main__":
    sys.exit(main())
