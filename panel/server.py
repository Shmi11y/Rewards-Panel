#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rewards 面板后端（便携版）

目录约定（全部相对应用根，不依赖 %LOCALAPPDATA%）：
    <root>/app       node.exe + dist + node_modules
    <root>/browser   patchright 的 chromium（PLAYWRIGHT_BROWSERS_PATH 指向这里）
    <root>/data      .env / config.json / sessions
    <root>/panel     本文件与 index.html
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlsplit
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = Path(os.environ.get("REWARDS_APP_ROOT") or Path(__file__).resolve().parent.parent)
HOME = ROOT / "app"
ENTRY = HOME / "dist" / "index.js"
LOG_DIR = HOME / "logs"
BROWSER_ROOT = ROOT / "browser"
USER_SCRIPT = ROOT / "browser-assistant" / "Rewards-Panel-userscript.user.js"
SCRIPT_INSTALL_PATH = f"/install/{secrets.token_urlsafe(24)}/Rewards-Panel.user.js"
TAMPERMONKEY_STORES = {
    "msedge": "https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd",
    "chrome": "https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo",
}
WINDOWS = os.name == "nt"
CONTAINER = not WINDOWS and os.environ.get("REWARDS_CONTAINER") == "1"


def _data_dir() -> Path:
    env = os.environ.get("REWARDS_DATA_DIR")
    if env:
        return Path(env).resolve()
    local = ROOT / "data"
    if local.exists() or not WINDOWS:
        return local
    return Path(os.environ.get("LOCALAPPDATA", "")) / "RewardsBot"


if not WINDOWS:
    os.umask(0o077)

DATA_DIR = _data_dir()
os.environ["REWARDS_DATA_DIR"] = str(DATA_DIR)

SOURCE_ROOT = str(Path(__file__).resolve().parent.parent)
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)
import accounts as acct
import balance
import schedule as sched_mod
import account_holds
import run_history
import diagnostics
import updates
from panel import browser_script
from windows_notifications import REWARDS_DASHBOARD, valid_run_id

NODE = run_history.node_path(ROOT)

SESSION_DB = DATA_DIR / "sessions" / "sessions.db"
CONFIG_FILE = DATA_DIR / "config.json"
PREFS_FILE = DATA_DIR / "prefs.json"
PREFS_DEFAULT = {"theme": "light", "onlyActive": False, "keepInTray": True}
_prefs_lock = threading.RLock()
_schedule_lock = threading.Lock()
_config_lock = threading.Lock()
TOKEN_FILE = DATA_DIR / ".api-token"


def _load_token() -> str:
    """本机接口令牌：只有拿到它才能调 API（防止同机其它程序驱动脚本）"""
    env = os.environ.get("REWARDS_TOKEN")
    if env:
        return env
    try:
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except Exception:
        pass
    tok = secrets.token_hex(16)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(tok, encoding="utf-8")
    except Exception:
        return ""
    return tok


TOKEN = _load_token()
HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("REWARDS_PORT", "8765"))

STR_KEYS = [
    "browserChannel",
    "searchSettings.searchDelay.min", "searchSettings.searchDelay.max",
    "searchSettings.readDelay.min", "searchSettings.readDelay.max",
    "searchSettings.searchResultVisitTime",
    "clusters",
]


CONFIG_KEYS = [
    # 运行
    "headless", "errorDiagnostics", "debugLogs", "compactLog",
    "ensureStreakProtection", "autoClaimPunchcardRewards",
    "skipNonPointTasks", "contintueOnBotWarning", "prewarmDesktopLogin",
    # 任务
    "workers.doDailySet", "workers.doClaimBonusPoints", "workers.doMorePromotions",
    "workers.doPunchCards", "workers.doAppPromotions", "workers.doDesktopSearch",
    "workers.doMobileSearch", "workers.doBonusSearches", "workers.doDailyCheckIn",
    "workers.doReadToEarn", "workers.doActivateSearchPerk", "workers.doVisualSearch",
    # 活动类型
    "activities.urlReward", "activities.searchOnBing",
    # 搜索
    "searchOnBingLocalQueries",
    "searchSettings.scrollRandomResults", "searchSettings.clickRandomResults",
    "searchSettings.runOnZeroPoints", "searchSettings.parallelSearching",
    "searchSettings.clusterSearch",
    # 实验
    "experimental.apiSearch", "experimental.apiSearchOnBing",
    "experimental.blockMedia", "experimental.edgeBrowsing",
    # 拟人化 / 代理
    "humanize.enabled", "humanize.skipWhenCompletedToday",
    "proxy.ignoreCertificateErrors",
]

ALL_KEYS = CONFIG_KEYS + STR_KEYS
BACKUP_FORMAT = 1
BACKUP_MAX_BYTES = 64 * 1024
SCHEDULE_BACKUP_KEYS = ("mode", "times", "retry", "weekdaysOnly", "catchUpMode",
                        "catchUpMinutes", "paused", "bootDelay")
UPDATE_DOWNLOADS = updates.UpdateDownloads(ROOT / "updates", ROOT)
_DURATION = re.compile(
    r"([0-9]+(?:\.[0-9]+)?|\.[0-9]+) *(milliseconds?|msecs?|ms|seconds?|secs?|s|"
    r"minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w|years?|yrs?|y)?", re.IGNORECASE)
_DURATION_UNITS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000,
                   "w": 604_800_000, "y": 31_557_600_000}
_DURATION_LIMIT = 2_147_483_647


def prune_old_logs(keep_days: int = 30) -> dict:
    """删除 app/logs 下超过保留期的日志，并给调度日志封顶"""
    result = run_history.maintain_daily_logs(LOG_DIR, keep_days) if run_state() is None else {"removed": 0, "freed": 0, "compressed": 0}
    sl = ROOT / "logs" / "scheduler.log"
    try:
        if sl.exists() and sl.stat().st_size > 2 * 1024 * 1024:
            lines = sl.read_text(encoding="utf-8", errors="ignore").splitlines()[-800:]
            sl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
    return result


def cache_size() -> int:
    total = 0
    for sub in (DATA_DIR / "login-profile", ROOT / "logs"):
        if sub.exists():
            for f in sub.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except Exception:
                    pass
    return total


def clean_cache() -> dict:
    """清缓存：先把目录改名（改名不受删除限制），再尽力删除

    沙箱/安全软件可能拦截批量删除并抛出 BaseException，
    所以这里一律用 BaseException 兜住，删不掉就留到下次启动再试。
    """
    import shutil as _sh
    freed, removed, pending = 0, 0, 0
    lp = DATA_DIR / "login-profile"
    if lp.exists():
        try:
            size = sum(f.stat().st_size for f in lp.rglob("*") if f.is_file())
            target = DATA_DIR / f"login-profile.old-{int(time.time())}"
            lp.rename(target)
            freed += size
            removed += 1
            try:
                _sh.rmtree(target)
            except BaseException:
                pending += 1
        except BaseException as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"清理失败: {exc}"}
    for pat in ("*.png", "*.jpg"):
        for f in (ROOT / "logs").glob(pat):
            try:
                freed += f.stat().st_size
                f.unlink()
                removed += 1
            except BaseException:
                pending += 1
    return {"ok": True, "removed": removed, "freed": freed, "pending": pending}


def purge_old_profiles() -> int:
    """启动时清掉上次没删干净的登录 profile"""
    import shutil as _sh
    n = 0
    for d in DATA_DIR.glob("login-profile.old-*"):
        try:
            _sh.rmtree(d)
            n += 1
        except BaseException:
            pass
    return n


def _read_prefs() -> dict:
    try:
        got = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        if isinstance(got, dict):
            return got
    except (OSError, ValueError):
        pass
    return {}


def get_prefs() -> dict:
    with _prefs_lock:
        stored = _read_prefs()
        return {key: stored.get(key, default) for key, default in PREFS_DEFAULT.items()}


def save_prefs(patch: dict) -> None:
    with _prefs_lock:
        stored = _read_prefs()
        stored.update(patch)
        _write_settings(PREFS_FILE, stored)


def risk_acknowledged() -> bool:
    with _prefs_lock:
        return _read_prefs().get("riskAcknowledged") is True


def set_prefs(patch: dict) -> dict:
    if not isinstance(patch, dict):
        return {"ok": False, "msg": "参数不对"}
    if "keepInTray" in patch and type(patch["keepInTray"]) is not bool:
        return {"ok": False, "msg": "托盘设置须为布尔值"}
    try:
        save_prefs({key: patch[key] for key in PREFS_DEFAULT if key in patch})
    except OSError as exc:
        return {"ok": False, "msg": str(exc)}
    return {"ok": True, "prefs": get_prefs()}


def all_actions(limit: int = 800) -> dict:
    full = parse_log()
    items = full.get("actions") or []
    n = max(1, min(int(limit or 800), 5000))
    return {"actions": items[-n:], "total": len(items),
            "balance": full.get("balance"), "startBalance": full.get("startBalance"),
            "gained": full.get("gained"), "quota": full.get("quota")}


def _dig(data: dict, path: str):
    node = data
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def get_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"items": {}, "error": "config.json 不存在"}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"items": {}, "error": str(exc)}
    return {"items": {k: _dig(data, k) for k in ALL_KEYS}}


def set_config(path: str, value) -> dict:
    if path not in ALL_KEYS:
        return {"ok": False, "msg": "不允许修改该项"}
    return set_config_batch({path: value})


def set_config_batch(items: dict) -> dict:
    if not isinstance(items, dict) or not items:
        return {"ok": False, "msg": "没有改动"}
    bad = [k for k in items if k not in ALL_KEYS]
    if bad:
        return {"ok": False, "msg": "不允许修改: " + ", ".join(bad[:3])}
    if not CONFIG_FILE.exists():
        return {"ok": False, "msg": "config.json 不存在"}
    try:
        with _config_lock:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            _merge_task_settings(data, items)
            _write_settings(CONFIG_FILE, data, indent=4)
        return {"ok": True, "changed": len(items)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": str(exc)}


def _merge_task_settings(data: dict, items: dict) -> None:
    for path, value in items.items():
        node = data
        parts = path.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value


def _replace_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staging = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            staging.replace(path)
        finally:
            stream.close()
            staging.unlink(missing_ok=True)


def _write_settings(path: Path, data: dict, indent: int = 2) -> None:
    _replace_file(path, json.dumps(data, ensure_ascii=False, indent=indent).encode("utf-8"))


def _duration_ms(value):
    if type(value) in (int, float):
        return value
    if isinstance(value, str) and len(value) <= 100 and (match := _DURATION.fullmatch(value)):
        number, unit = match.groups()
        unit = (unit or "ms").lower()
        multiplier = 1 if unit.startswith(("ms", "millisecond")) else _DURATION_UNITS[unit[0]]
        return float(number) * multiplier
    return -1


def _validate_delay_ranges(tasks: dict) -> None:
    for delay in ("searchDelay", "readDelay"):
        lo = tasks.get(f"searchSettings.{delay}.min")
        hi = tasks.get(f"searchSettings.{delay}.max")
        if lo is not None and hi is not None and _duration_ms(lo) > _duration_ms(hi):
            raise ValueError(f"最大间隔不能小于最小间隔（{delay}）")


def _validate_backup(body: dict) -> None:
    if (not isinstance(body, dict) or set(body) != {"formatVersion", "tasks", "schedule", "interface"}
            or type(body["formatVersion"]) is not int or body["formatVersion"] != BACKUP_FORMAT):
        raise ValueError("无法识别的设置备份格式或版本")
    tasks, plan, prefs = body["tasks"], body["schedule"], body["interface"]
    if not WINDOWS and isinstance(plan, dict) and plan.get("mode") == "boot":
        raise ValueError("此备份使用 Windows 开机计划，请先在原应用改为每日定时或手动")
    if not isinstance(tasks, dict) or set(tasks).difference(ALL_KEYS):
        raise ValueError("备份包含不支持的任务设置")
    for key, value in tasks.items():
        if key in CONFIG_KEYS:
            valid = type(value) is bool
        elif key == "clusters":
            valid = type(value) is int and 1 <= value <= 8
        elif key == "browserChannel":
            valid = value in ("", "chromium", "chrome", "msedge")
        else:
            valid = 0 <= _duration_ms(value) <= _DURATION_LIMIT
        if not valid:
            raise ValueError(f"任务设置格式或取值不合法（{key}）")
    _validate_delay_ranges(tasks)

    if (not isinstance(plan, dict) or not set(SCHEDULE_BACKUP_KEYS) <= set(plan)
            or set(plan).difference(SCHEDULE_BACKUP_KEYS, ("pausedUntil", "groups"))
            or plan["mode"] not in ("manual", "daily", "boot")
            or not isinstance(plan["times"], list) or not 1 <= len(plan["times"]) <= sched_mod.MAX_TIMES
            or any(not isinstance(slot, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", slot)
                   for slot in plan["times"])
            or len(set(plan["times"])) != len(plan["times"])
            or type(plan["retry"]) is not int or not 0 <= plan["retry"] <= 2
            or type(plan["weekdaysOnly"]) is not bool or type(plan["paused"]) is not bool
            or plan["catchUpMode"] not in ("latest", "skip")
            or type(plan["catchUpMinutes"]) is not int
            or not 1 <= plan["catchUpMinutes"] <= sched_mod.MAX_CATCH_UP_MINUTES
            or type(plan["bootDelay"]) is not int or not 0 <= plan["bootDelay"] <= 86_400
            or ("pausedUntil" in plan and (not plan["paused"] or plan["mode"] == "manual"
                                           or not run_history.valid_pause_deadline(plan["pausedUntil"])))):
        raise ValueError("定时计划格式或取值不合法")
    groups = plan.get("groups", {})
    if not isinstance(groups, dict) or any(slot not in plan["times"] or not sched_mod.valid_group(group)
                                           for slot, group in groups.items()):
        raise ValueError("定时分组格式不合法")
    if (not isinstance(prefs, dict) or set(prefs) != set(PREFS_DEFAULT)
            or prefs["theme"] not in ("light", "dark")
            or any(type(prefs[key]) is not bool for key in ("onlyActive", "keepInTray"))):
        raise ValueError("界面偏好格式或取值不合法")


def export_settings() -> dict:
    with _config_lock, _prefs_lock, _schedule_lock:
        config = get_config()
        if "error" in config:
            return {"ok": False, "msg": "无法读取任务设置"}
        schedule = sched_mod.load()
        backup = {
            "formatVersion": BACKUP_FORMAT,
            "tasks": {key: value for key, value in config["items"].items() if value is not None},
            "schedule": {key: schedule[key] for key in SCHEDULE_BACKUP_KEYS},
            "interface": get_prefs(),
        }
        if "pausedUntil" in schedule:
            backup["schedule"]["pausedUntil"] = schedule["pausedUntil"]
        if schedule["groups"]:
            backup["schedule"]["groups"] = schedule["groups"]
        try:
            _validate_backup(backup)
        except ValueError as exc:
            return {"ok": False, "msg": str(exc)}
        return {"ok": True, "backup": backup}


def import_settings(backup: dict) -> dict:
    try:
        _validate_backup(backup)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}

    with _config_lock, _prefs_lock, _schedule_lock:
        saved_files = []
        try:
            original_config = CONFIG_FILE.read_bytes()
            config = json.loads(original_config)
            original_prefs = PREFS_FILE.read_bytes() if PREFS_FILE.exists() else None
            prefs = _read_prefs()
            plan = sched_mod.load()
            previous_plan = {key: plan[key] for key in SCHEDULE_BACKUP_KEYS}
            if "pausedUntil" in plan:
                previous_plan["pausedUntil"] = plan["pausedUntil"]
            previous_plan["groups"] = plan["groups"]
            imported_plan = {**backup["schedule"], "times": sorted(backup["schedule"]["times"]),
                             "groups": backup["schedule"].get("groups", {})}
            plan_changed = imported_plan != previous_plan

            if backup["tasks"]:
                _merge_task_settings(config, backup["tasks"])
                try:
                    _validate_delay_ranges({key: _dig(config, key) for key in STR_KEYS})
                except ValueError as exc:
                    return {"ok": False, "msg": str(exc)}
                _write_settings(CONFIG_FILE, config, indent=4)
                saved_files.append((CONFIG_FILE, original_config))
            prefs.update(backup["interface"])
            _write_settings(PREFS_FILE, prefs)
            saved_files.append((PREFS_FILE, original_prefs))
            if plan_changed:
                plan.pop("pausedUntil", None)
                plan.update(imported_plan)
                plan["activeAfter"] = time.time()
                sched_mod.save(plan)
        except (OSError, ValueError, TypeError, AttributeError):
            rollback_failed = False
            for path, original in reversed(saved_files):
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        _replace_file(path, original)
                except OSError:
                    rollback_failed = True
            msg = "导入中断，部分设置可能已改变" if rollback_failed else "无法保存设置，导入已取消"
            return {"ok": False, "msg": msg}

        warning = None
        if plan_changed:
            mode = plan["mode"]
            try:
                sched_mod.sync_automation(mode)
            except (OSError, subprocess.SubprocessError):
                warning = "设置已导入，自动运行未能重新配置。请在「自动运行」中检查。"
        status = sched_mod.status()
        if plan_changed and (status["boot"] != (mode == "boot") or status["daily"] != (mode == "daily")
                             or status["daemonAlive"] != (mode == "daily")):
            warning = "设置已导入，自动运行未能重新配置。请在「自动运行」中检查。"
        return {"ok": True, "prefs": get_prefs(), "schedule": status, "warning": warning}


def install_browser() -> dict:
    if CONTAINER:
        return {"ok": False, "msg": "Chromium 随 Docker 镜像提供，请重新构建或更新镜像"}
    node = NODE
    cli = HOME / "node_modules" / "patchright" / "cli.js"
    if node is None or not node.is_file() or not cli.is_file():
        return {"ok": False, "msg": "找不到 Node.js 或 patchright/cli.js"}
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_ROOT)
    (ROOT / "logs").mkdir(exist_ok=True)
    fh = open(ROOT / "logs" / "browser-install.log", "ab")
    try:
        with fh:
            subprocess.Popen(
                [str(node), str(cli), "install", "chromium"],
                cwd=str(HOME), env=env,
                stdout=fh, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": str(exc)}
    return {"ok": True, "msg": "已开始下载内置内核，约 184MB"}


RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def read_logs(count: int = 300, source: str = "task") -> dict:
    paths = {
        "task": LOG_DIR / f"{datetime.now():%Y-%m-%d}.log",
        "startup": ROOT / "logs" / "launcher.log",
        "browser": ROOT / "logs" / "browser-install.log",
        "scheduler": ROOT / "logs" / "scheduler.log",
    }
    path = paths.get(source)
    if path is None:
        return {"lines": [], "error": "未知日志来源", "count": 0}
    if not path.exists():
        return {"lines": [], "path": str(path), "count": 0}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            tail = deque(fh, maxlen=max(20, min(count, 2000)))
    except Exception as exc:  # noqa: BLE001
        return {"lines": [], "path": str(path), "error": str(exc), "count": 0}
    out = []
    for raw in tail:
        text = RE_ANSI.sub("", raw.rstrip("\r\n"))
        up = text.upper()
        if "[ERROR]" in up or "错误" in text:
            lvl = "error"
        elif "[WARN]" in up or "[WARNING]" in up or "警告" in text:
            lvl = "warn"
        elif "成功" in text or "POINTSGAINED" in up or "已完成" in text:
            lvl = "ok"
        else:
            lvl = "info"
        out.append({"lvl": lvl, "text": text})
    return {"lines": out, "path": str(path), "count": len(out)}


def _node_version() -> tuple[bool, str]:
    node = NODE
    if node is None or not node.is_file():
        return False, "缺少 Node.js"
    try:
        res = subprocess.run([str(node), "-v"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (res.stdout or res.stderr).strip()
        return res.returncode == 0, out or "无输出"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:80]


def system_browsers() -> dict:
    if not WINDOWS:
        return {"chrome": shutil.which("google-chrome") or "", "msedge": shutil.which("microsoft-edge") or "", "chromium": ""}
    candidates = [path for path in BROWSER_ROOT.glob("chromium-*/chrome-win64/chrome.exe") if path.is_file()]
    chromium = max(candidates, key=lambda path: int(path.parents[1].name.split("-")[-1])
                   if path.parents[1].name.split("-")[-1].isdigit() else -1, default=None)
    return {**run_history.windows_browser_paths(), "chromium": str(chromium) if chromium else ""}


def browser_preferences() -> dict:
    browsers = system_browsers()
    channel = run_history.configured_browser_channel(DATA_DIR)
    return {"ok": True, "browser": channel or "default", "defaultChannel": run_history.windows_default_browser_channel(),
            "available": {"default": True, **{name: bool(value) for name, value in browsers.items()}},
            "scriptUrl": f"http://127.0.0.1:{PORT}{SCRIPT_INSTALL_PATH}" if USER_SCRIPT.is_file() else "",
            "scriptAutomation": browser_script.state(DATA_DIR)}


def open_tampermonkey_store() -> dict:
    if not WINDOWS or run_history.participation(DATA_DIR) != "browser":
        return {"ok": False, "msg": "请先切换到油猴脚本模式"}
    browser = run_history.configured_browser_channel(DATA_DIR) or run_history.windows_default_browser_channel()
    url = TAMPERMONKEY_STORES.get(browser)
    if not url:
        return {"ok": False, "msg": "请到「参与模式」选择 Edge 或 Chrome 后再打开官方商店。"}
    return open_selected_browser(url, browser=browser)


def open_selected_browser(url: str, *, browser: str | None = None) -> dict:
    name = browser if browser is not None else run_history.configured_browser_channel(DATA_DIR)
    if not name:
        try:
            os.startfile(url)
        except OSError:
            name = "chromium"
        else:
            return {"ok": True}
    program = system_browsers().get(name)
    if not program:
        return {"ok": False, "msg": "找不到所选浏览器，请在「参与模式」中选择已安装的浏览器。"}
    args = [program]
    if name == "chromium":
        args.append(f"--user-data-dir={ROOT / 'browser-profile'}")
    try:
        subprocess.Popen([*args, url], cwd=str(ROOT), creationflags=run_history.NO_WINDOW)
    except OSError:
        return {"ok": False, "msg": "无法打开所选浏览器，请确认浏览器可正常启动。"}
    return {"ok": True}


def set_browser_preference(value) -> dict:
    if not WINDOWS or not isinstance(value, str):
        return {"ok": False, "msg": "浏览器设置无效"}
    if value != "default" and not system_browsers().get(value):
        return {"ok": False, "msg": "请选择已安装的浏览器。"}
    return set_config("browserChannel", "" if value == "default" else value)


def selftest() -> dict:
    checks = []
    ok, detail = _node_version()
    checks.append({"name": "Node 运行时", "ok": ok, "detail": detail})

    checks.append({"name": "程序入口", "ok": ENTRY.exists(), "detail": str(ENTRY)})

    browser = browser_version()
    checks.append({"name": "内置内核", "ok": browser != "未安装", "detail": browser})

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        workers = data.get("workers", {})
        on = sum(1 for v in workers.values() if v is True)
        checks.append({"name": "配置文件", "ok": True,
                       "detail": f"可解析，{len(workers)} 个任务项，已开 {on}"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "配置文件", "ok": False, "detail": str(exc)[:80]})

    accs = [a for a in acct.load()["accounts"] if a.get("enabled", True)]
    checks.append({"name": "可用账号", "ok": bool(accs),
                   "detail": f"{len(accs)} 个启用" if accs else "没有启用的账号"})

    sess = acct.summary()
    logged = sum(1 for i in sess["items"] if i["loggedIn"])
    checks.append({"name": "登录会话", "ok": logged > 0,
                   "detail": f"{logged} 个已登录" if logged else "全部需要重新登录"})

    pyw = sched_mod.find_pythonw()
    checks.append({"name": "定时守护依赖", "ok": bool(pyw), "detail": pyw or "未找到 Python 运行时"})

    sysb = system_browsers()
    checks.append({"name": "系统浏览器", "ok": True,
                   "detail": "Chrome " + ("有" if sysb["chrome"] else "无")
                             + " / Edge " + ("有" if sysb["msedge"] else "无")})

    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "version": _app_version(), "portableVersion": portable_version(), "dataDir": str(DATA_DIR),
            "appDir": str(HOME), "root": str(ROOT)}


def portable_version() -> str | None:
    try:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        match = updates.RELEASE_VERSION.fullmatch(version)
        return ".".join(match.groups()) if match else None
    except (OSError, UnicodeError):
        return None


def _app_version() -> str:
    try:
        pkg = json.loads((HOME / "package.json").read_text(encoding="utf-8"))
        return str(pkg.get("version", ""))
    except Exception:
        return ""

_proc: subprocess.Popen | None = None
_lock = threading.RLock()
_cache = {"key": None, "data": None}
_ui_state: dict = {}
_local_close = None   # 由桌面外壳注入，用于关闭窗口
_local_open = None
_local_exit = None
_window_run_lock = threading.Lock()
_window_run: int | None = None
_stop_mark: float = 0.0   # 上次手动停止时日志的 mtime，用来压掉"刚跑完仍显示运行中"
_balance_lock = threading.Lock()
_balance_jobs: dict[str, dict] = {}


def start_balance_sync() -> dict:
    accounts = acct.load()
    email = accounts.get("active")
    if not email:
        return {"ok": False, "msg": "请先选择账号"}
    with _balance_lock:
        if any(job["state"] == "syncing" for job in _balance_jobs.values()):
            return {"ok": False, "msg": "已有余额同步正在进行"}
        _balance_jobs[email] = {"state": "syncing"}

    def worker() -> None:
        try:
            points = balance.read(SESSION_DB, email)
            now = time.time()
            balance.save(DATA_DIR, email, points, now, "online")
            result = {"state": "ready", "balance": points, "at": now}
        except ValueError as error:
            result = {"state": "failed", "message": str(error) if type(error) is ValueError else "无法读取登录会话"}
        except (OSError, sqlite3.Error, TypeError, AttributeError):
            result = {"state": "failed", "message": "无法读取登录会话，请重试"}
        with _balance_lock:
            _balance_jobs[email] = result

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


def queue_window_run(run_id: int) -> None:
    global _window_run
    with _window_run_lock:
        _window_run = run_id


def take_window_run() -> int | None:
    global _window_run
    with _window_run_lock:
        run_id = _window_run
        _window_run = None
        return run_id


RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+ ")
RE_TITLE = re.compile(r'标题="([^"]*)"')
RE_GAIN = re.compile(r"(?:pointsGained|获得积分)=(\d+)\s*\|\s*(?:currentBalance|当前余额|现余额)=(\d+)")
RE_BALANCE = re.compile(r"(?:currentBalance|当前余额|现余额|可用积分)\s*(?:=|:)\s*(\d+)")
RE_QUOTA = re.compile(r"今日可赚取 \| 移动端: (\d+) \| 浏览器: (\d+) \| App: (\d+)")
RE_PHASE = re.compile(r"\[(DAILY-SET|MORE-PROMOTIONS|SEARCH|LOGIN|BOOTSTRAP|PUNCH)[^\]]*\]\s*(.*)")
RE_ERROR = re.compile(r"\[ERROR\]")
RE_TAG = re.compile(r"\[(CLAIM-BONUS-POINTS|DAILY-CHECK-IN|READ-TO-EARN|DAILY-SET|MORE-PROMOTIONS|URL-REWARD|SEARCH)\]")
TAG_NAMES = {
    "CLAIM-BONUS-POINTS": "领取奖励积分",
    "DAILY-CHECK-IN": "每日签到",
    "READ-TO-EARN": "阅读赚积分",
    "DAILY-SET": "每日任务集",
    "MORE-PROMOTIONS": "更多推广",
    "URL-REWARD": "访问奖励",
    "SEARCH": "搜索",
}


def log_path() -> Path:
    return LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"


def proc_alive() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


_pid_alive = run_history.process_alive


def task_pid() -> int:
    try:
        return int((DATA_DIR / "task.pid").read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def run_state() -> str | None:
    """process=面板启动的 / task=定时守护启动的 / recent=日志仍在动 / None=空闲"""
    if proc_alive():
        return "process"
    pid = task_pid()
    if pid:
        return "task" if _pid_alive(pid) else None
    path = log_path()
    if path.exists():
        m = path.stat().st_mtime
        if m > _stop_mark and (time.time() - m) < 150:
            return "recent"
    return None


def parse_log() -> dict:
    path = log_path()
    if not path.exists():
        return {"actions": [], "balance": _latest_logged_balance(), "startBalance": None, "gained": 0,
                "actionCount": 0, "quota": None, "phase": "未开始", "errors": [],
                "mtime": 0, "lines": 0}

    stat = path.stat()
    key = (str(path), stat.st_mtime, stat.st_size)
    if _cache["key"] == key and _cache["data"]:
        return _cache["data"]

    actions: list[dict] = []
    errors: list[str] = []
    balance = start_balance = quota = None
    phase = "运行中"
    pending_title = ""
    count = 0

    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            count += 1
            ts = RE_TS.match(line)
            stamp = ts.group(1)[11:] if ts else ""

            m = RE_TITLE.search(line)
            if m:
                pending_title = m.group(1)

            m = RE_GAIN.search(line)
            if m:
                gained, bal = int(m.group(1)), int(m.group(2))
                if start_balance is None:
                    start_balance = bal - gained
                balance = bal
                title = pending_title
                if not title:
                    t = RE_TAG.search(line)
                    title = TAG_NAMES.get(t.group(1), "") if t else ""
                actions.append({"time": stamp, "title": title or "未命名活动",
                                "points": gained, "balance": bal})
                pending_title = ""

            m = RE_QUOTA.search(line)
            if m:
                quota = {"mobile": int(m.group(1)), "browser": int(m.group(2)),
                         "app": int(m.group(3))}

            m = RE_PHASE.search(line)
            if m and m.group(2).strip():
                phase = m.group(2).strip()[:40]

            if RE_ERROR.search(line):
                errors.append(line.strip()[:160])

    if balance is None:
        balance = _latest_logged_balance()

    data = {
        "actions": actions,
        "actionCount": len(actions),
        "balance": balance,
        "startBalance": start_balance,
        "gained": sum(a["points"] for a in actions),
        "quota": quota,
        "phase": phase,
        "errors": errors[-5:],
        "mtime": int(stat.st_mtime),
        "lines": count,
    }
    _cache["key"] = key
    _cache["data"] = data
    return data


def session_info() -> dict:
    if not SESSION_DB.exists():
        return {"exists": False}
    info = {"exists": True, "size": SESSION_DB.stat().st_size,
            "mtime": int(SESSION_DB.stat().st_mtime), "rows": []}
    try:
        con = sqlite3.connect(f"file:{SESSION_DB}?mode=ro", uri=True, timeout=2)
        for platform, ln, upd in con.execute(
                "SELECT platform, length(storage_state), updated_at FROM sessions"):
            info["rows"].append({
                "platform": platform,
                "kb": round((ln or 0) / 1024, 1),
                "ageHours": round((time.time() * 1000 - (upd or 0)) / 3600000, 1),
            })
        con.close()
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return info


def env_summary() -> dict:
    p = DATA_DIR / ".env"
    out = {"email": None, "hasPassword": False, "hasTotp": False}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        t = line.strip()
        if not t or t.startswith("#") or "=" not in t:
            continue
        k, _, v = t.partition("=")
        k, v = k.strip(), v.strip()
        if k == "ACCOUNT_1_EMAIL":
            out["email"] = v or None
        elif k == "ACCOUNT_1_PASSWORD":
            out["hasPassword"] = bool(v)
        elif k == "ACCOUNT_1_TOTP_SECRET":
            out["hasTotp"] = bool(v)
    return out


def browser_version() -> str:
    found = sorted(BROWSER_ROOT.glob("chromium-*")) if BROWSER_ROOT.exists() else []
    return found[-1].name if found else "未安装"


def mark_stopped() -> None:
    global _stop_mark
    path = log_path()
    try:
        _stop_mark = path.stat().st_mtime if path.exists() else 0.0
    except Exception:
        _stop_mark = 0.0


def _slim_log() -> dict:
    """首页只回最近 40 条动作，全量走 /api/actions"""
    full = parse_log()
    out = dict(full)
    out["actions"] = list(full.get("actions") or [])[-40:]
    return out


def _latest_logged_balance() -> int | None:
    """从当前日志和最近运行日志中找最后一个已记录的余额。"""
    paths = list(LOG_DIR.glob("*.log")) + list(LOG_DIR.glob("*.log.gz"))
    run_logs = DATA_DIR / "run-logs"
    if run_logs.exists():
        paths.extend(run_logs.glob("*.log"))
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            if path.suffix == ".gz":
                with run_history.open_daily_log(path.with_suffix("")) as stream:
                    lines = stream.read().splitlines()
            else:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except (OSError, EOFError):
            continue
        for line in reversed(lines):
            match = RE_BALANCE.search(line)
            if match:
                return int(match.group(1))
    return None


def build_status() -> dict:
    state = run_state()
    schedule = sched_mod.status()
    try:
        latest = run_history.read(DATA_DIR, latest=True)
        history = run_history.read(DATA_DIR)
    except sqlite3.Error:
        latest = history = []
    active_run = next((run for run in history if run["status"] == "running"), None)
    accounts = acct.summary()
    failures = {}
    for run in history:
        for row in run["accounts"]:
            if row["status"] in {"failed", "needs_login"}:
                failures.setdefault(row["email"].casefold(), run["finishedAt"])
    for row in accounts["items"]:
        row["lastFailure"] = failures.get(row["email"].casefold())
    with _balance_lock:
        synced = dict(_balance_jobs.get(accounts.get("active"), {}))
    try:
        notices = run_history.read_notifications(DATA_DIR)
    except (OSError, sqlite3.Error):
        notices = {"unread": 0, "muted": []}
    return {
        "running": state is not None,
        "runningBy": state,
        "orphan": (state == "task" and not proc_alive()),
        "pid": _proc.pid if proc_alive() else None,
        "now": datetime.now().strftime("%H:%M:%S"),
        "log": _slim_log(),
        "session": session_info(),
        "env": env_summary(),
        "accounts": accounts,
        "schedule": schedule,
        "scriptAutomation": browser_script.state(DATA_DIR) if WINDOWS and schedule["participation"] == "browser" else None,
        "riskAcknowledged": risk_acknowledged(),
        "ui": dict(_ui_state),
        "cacheBytes": cache_size(),
        "latestRun": active_run or (latest[0] if latest else None),
        "progress": run_history.progress(DATA_DIR, active_run, history) if active_run else None,
        "balanceSync": synced,
        "noticeCount": notices["unread"],
        "noticeMuted": notices["muted"],
        "app": {
            "home": str(HOME),
            "root": str(ROOT),
            "nodeOk": NODE is not None and NODE.is_file(),
            "platform": "windows" if WINDOWS else "docker" if CONTAINER else "linux",
            "entryOk": ENTRY.exists(),
            "browser": browser_version(),
        },
    }


def start_run(only: str | list[str] | None = None, *, retry_account: bool = False) -> dict:
    global _proc
    with _lock:
        if run_history.participation(DATA_DIR) != "automatic":
            return {"ok": False, "msg": "当前仅查看和提醒，请到微软 Rewards 完成活动"}
        if _proc is not None and _proc.poll() is None:
            return {"ok": False, "msg": "已经在运行中"}
        # 定时守护可能已经拉起任务，避免同时跑两份抢同一个账号
        if run_state() is not None:
            return {"ok": False, "msg": "已有任务在运行（可能由定时任务启动）"}
        _cache["key"] = None
        env = dict(os.environ)
        env["REWARDS_DATA_DIR"] = str(DATA_DIR)
        if only:
            try:
                emails = [only] if isinstance(only, str) else only
                emails = [email for email in emails if not account_holds.get(DATA_DIR, email)]
                if not emails:
                    return {"ok": False, "msg": "所选账号已暂停，请到微软官网确认后手动恢复"}
                target_env = acct.child_env_for_accounts(emails)
            except OSError:
                return {"ok": False, "msg": "无法读取账号配置"}
            if target_env is None:
                return {"ok": False, "msg": "此账号已移除或停用"}
            env.update(target_env)
            env["REWARDS_SELECTED_ACCOUNTS"] = json.dumps([
                target_env[f"ACCOUNT_{slot}_EMAIL"].lower() for slot in range(1, len(emails) + 1)
            ], ensure_ascii=False)
            if retry_account:
                env["REWARDS_RETRY_ACCOUNT"] = "1"
        else:
            try:
                acct.write_env()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "msg": f"生成 .env 失败: {exc}"}
        try:
            _proc = run_history.launch("manual", env=env)
        except OSError:
            return {"ok": False, "msg": "无法启动任务进程"}
        if _proc is None:
            return {"ok": False, "msg": "已有任务在运行（可能由定时任务启动）"}
        return {"ok": True, "pid": _proc.pid}


def run_record(run_id: str) -> dict | None:
    if not re.fullmatch(r"[1-9][0-9]{0,17}", run_id):
        return None
    records = run_history.read(DATA_DIR, run_id=int(run_id))
    return records[0] if records else None


def run_account_action(run_id: str, action: str, body) -> dict:
    email = body.get("email") if isinstance(body, dict) else None
    run = run_record(run_id)
    if not run:
        return {"ok": False, "msg": "找不到该运行记录"}
    if action == "retry-all":
        emails = [row["email"] for row in run["accounts"] if row["status"] == "failed"]
        if run["status"] == "running" or not emails:
            return {"ok": False, "msg": "本次运行没有可重试的失败账号"}
        enabled = {row["email"].casefold() for row in acct.load()["accounts"] if row.get("enabled", True)}
        targets = [email for email in emails if email.casefold() in enabled
                   and not account_holds.get(DATA_DIR, email)]
        return start_run(targets, retry_account=True) if targets else {"ok": False, "msg": "失败账号已移除、停用或暂停"}
    expected = "needs_login" if action == "login" else "failed"
    if run["status"] == "running" or not any(
        row["email"] == email and row["status"] == expected for row in run["accounts"]
    ):
        return {"ok": False, "msg": "该账号结果不支持此操作"}
    if action == "retry":
        return start_run(email, retry_account=True)
    with _lock:
        if run_state() is not None:
            return {"ok": False, "msg": "请等当前任务结束后再重新登录"}
        return acct.start_login(email, existing_only=True)


def stop_run() -> dict:
    with _lock:
        tracked = run_history.request_stop(DATA_DIR)
        if tracked is not None:
            if tracked["ok"]:
                mark_stopped()
            return tracked
        if proc_alive():
            return {"ok": False, "msg": "任务正在准备或收尾，请稍后再试"}
        # Also stop a live engine started by an older scheduler.
        ext = task_pid()
        if not ext or not _pid_alive(ext):
            return {"ok": False, "msg": "当前没有在跑"}
        try:
            if not WINDOWS or not run_history.stop_engine(ext):
                return {"ok": False, "msg": "结束进程失败"}
            (DATA_DIR / "task.pid").unlink(missing_ok=True)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "msg": "结束进程失败"}
        mark_stopped()
        return {"ok": True, "stopped": ext}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        if getattr(self, "_set_token", False) and TOKEN:
            self.send_header("Set-Cookie", f"rw_token={TOKEN}; Path=/; HttpOnly; SameSite=Strict")
            self._set_token = False
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            return WINDOWS
        try:
            if not WINDOWS and urlsplit("//" + self.headers.get("Host", "")).hostname not in (
                "127.0.0.1", "localhost", "::1",
            ):
                return False
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "rw_token" in cookie and secrets.compare_digest(cookie["rw_token"].value.encode(), TOKEN.encode()):
                return True
            if self.command == "GET" and parse_qs(urlsplit(self.path).query).get("k", [""])[0] == TOKEN:
                self._set_token = True
                return True
        except (CookieError, ValueError):
            pass
        return False

    def _strip_token_param(self) -> None:
        """把 ?k=令牌 从路径里去掉，后面路由照常匹配"""
        if "k=" not in self.path:
            return
        try:
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            u = urlparse(self.path)
            q = {k: v for k, v in parse_qs(u.query).items() if k != "k"}
            self.path = urlunparse(("", "", u.path or "/", "", urlencode(q, doseq=True), ""))
        except Exception:
            pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == SCRIPT_INSTALL_PATH:
            if not WINDOWS or self.headers.get("Host") != f"127.0.0.1:{PORT}":
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            try:
                source = USER_SCRIPT.read_text(encoding="utf-8")
                bridge = json.dumps({"url": f"http://127.0.0.1:{PORT}{browser_script.BRIDGE_PATH}",
                                     "token": browser_script.token(DATA_DIR)})
                content = source.replace("const PANEL_BRIDGE = null;", f"const PANEL_BRIDGE = {bridge};", 1).encode("utf-8")
            except OSError:
                self._send(404, b"not found", "text/plain; charset=utf-8")
            else:
                self._send(200, content, "text/plain; charset=utf-8")
            return
        if not self._authorized():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        self._strip_token_param()
        try:
            self._route_get()
        except Exception as exc:  # noqa: BLE001
            try:
                self._send(500, json.dumps({"ok": False, "msg": str(exc)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception:
                pass

    def _route_get(self) -> None:
        if self.path == "/api/browser/preferences":
            result = browser_preferences() if WINDOWS else {"ok": False, "msg": "浏览器设置适用于 Windows"}
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path == "/api/about/licenses":
            documents = []
            for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
                path = ROOT / name
                if path.is_file():
                    documents.append(path.read_text(encoding="utf-8"))
            self._send(200, "\n\n".join(documents).encode("utf-8"), "text/plain; charset=utf-8")
            return
        if self.path == "/api/diagnostics":
            report = diagnostics.make_report(ROOT, DATA_DIR, portable_version(), _app_version())
            self._send(200, json.dumps({"report": report}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path in ("/api/update/check", "/api/update/status", "/api/settings/backup"):
            if self.path == "/api/update/check":
                result = UPDATE_DOWNLOADS.check(portable_version())
                if not WINDOWS:
                    result["canDownload"] = False
                if result.get("updateAvailable"):
                    run_history.record_notice(DATA_DIR, "update", "发现新版本",
                                              f"Rewards Panel v{result['latest']} 可下载",
                                              event=f"update:{result['latest']}")
            elif self.path == "/api/update/status":
                result = UPDATE_DOWNLOADS.status()
            else:
                result = export_settings()
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/status"):
            status = build_status()
            if self.path == "/api/status?desktop=1":
                status["windowRun"] = take_window_run()
            self._send(200, json.dumps(status, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path == "/api/runs":
            self._send(200, json.dumps({"runs": run_history.read(DATA_DIR)}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path == "/api/update/rollback":
            self._send(200, json.dumps(updates.rollback_status(ROOT), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path == "/api/notifications":
            self._send(200, json.dumps(run_history.read_notifications(DATA_DIR), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if match := re.fullmatch(r"/api/runs/([^/]+)/log", self.path):
            run = run_record(match[1])
            result = {"ok": False, "msg": "找不到该运行记录"}
            if run:
                try:
                    result = {"ok": True, **run_history.read_log(DATA_DIR, run, LOG_DIR)}
                    result["text"] = RE_ANSI.sub("", result["text"])
                except OSError:
                    result = {"ok": False, "msg": "无法读取本次日志"}
            self._send(200 if run else 404, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/accounts"):
            self._send(200, json.dumps(acct.summary(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/config"):
            self._send(200, json.dumps(get_config(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/schedule"):
            self._send(200, json.dumps(sched_mod.status(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if urlsplit(self.path).path == "/api/logs":
            query = parse_qs(urlsplit(self.path).query)
            try:
                n = int(query.get("lines", ["300"])[0])
            except ValueError:
                n = 300
            source = query.get("source", ["task"])[0]
            self._send(200, json.dumps(read_logs(n, source), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/prefs"):
            self._send(200, json.dumps(get_prefs(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/actions"):
            n = 800
            if "limit=" in self.path:
                try:
                    n = int(self.path.split("limit=", 1)[1].split("&")[0])
                except Exception:
                    n = 800
            self._send(200, json.dumps(all_actions(n), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/selftest"):
            self._send(200, json.dumps(selftest(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/login-status"):
            job = ""
            if "job=" in self.path:
                job = self.path.split("job=", 1)[1].split("&")[0]
            self._send(200, json.dumps(acct.job_status(job), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if self.path.startswith("/assets/"):
            rel = self.path[len("/assets/"):].split("?")[0]
            base = (ROOT / "assets").resolve()
            target = (base / rel).resolve()
            if target.is_file() and base in target.parents:
                ctype = {".png": "image/png", ".ico": "image/x-icon",
                         ".svg": "image/svg+xml"}.get(target.suffix.lower(),
                                                      "application/octet-stream")
                self._send(200, target.read_bytes(), ctype)
                return
        if self.path in ("/", "/index.html", "/?desktop=1"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path == browser_script.BRIDGE_PATH:
            self._route_script_bridge()
            return
        if not self._authorized():
            self._send(403, b'{"ok": false, "msg": "forbidden"}', "application/json; charset=utf-8")
            return
        self._strip_token_param()
        try:
            self._route_post()
        except Exception as exc:  # noqa: BLE001
            try:
                self._send(500, json.dumps({"ok": False, "msg": str(exc)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception:
                pass

    def _route_script_bridge(self) -> None:
        def reply(code: int, result: dict) -> None:
            self._send(code, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        supplied = self.headers.get("X-Rewards-Script", "")
        if (not WINDOWS or self.headers.get("Host") != f"127.0.0.1:{PORT}"
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", supplied)
                or not secrets.compare_digest(supplied, browser_script.token(DATA_DIR))):
            reply(403, {"ok": False})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if self.headers.get_content_type() != "application/json" or not 0 < size <= 1024:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError()
        except ValueError:
            reply(400, {"ok": False})
            return
        action = body.get("action")
        active = run_history.participation(DATA_DIR) == "browser"
        result = {"ok": True, "active": active}
        if action == "status":
            if active:
                result.update(browser_script.state(DATA_DIR))
        elif action == "claim" and active:
            ticket = body.get("ticket")
            if ticket is not None and (not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", ticket)):
                reply(400, {"ok": False})
                return
            paused = ticket is not None and sched_mod.load()["paused"]
            result["started"] = not paused and browser_script.claim(DATA_DIR, ticket)
        elif action == "hold" and active:
            reason = body.get("reason")
            if not isinstance(reason, str) or reason not in browser_script.HOLD_REASONS:
                reply(400, {"ok": False})
                return
            if browser_script.hold(DATA_DIR, reason):
                threading.Thread(target=run_history.notify_browser_script_hold, args=(DATA_DIR, reason), daemon=True).start()
        else:
            reply(400, {"ok": False})
            return
        reply(200, result)

    def _route_post(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if self.path == "/api/settings/backup" and not 0 < length <= BACKUP_MAX_BYTES:
            result = {"ok": False, "msg": "请选择不超过 64 KB 的 JSON 设置备份"}
            self._send(413, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

        if self.path != "/api/update/install" and UPDATE_DOWNLOADS.installing():
            result = {"ok": False, "msg": "正在安装更新，请等待面板重新打开"}
        elif self.path == "/api/settings/backup":
            result = import_settings(body)
        elif self.path == "/api/update/download":
            result = (UPDATE_DOWNLOADS.start(body.get("tag") if isinstance(body, dict) else None)
                      if WINDOWS else {"ok": False, "msg": "请按 Linux 或 Docker 的升级步骤更新"})
        elif self.path == "/api/update/install":
            if not _local_exit:
                result = {"ok": False, "msg": "请从便携版桌面窗口启动更新"}
            elif run_state() in {"process", "task"} or acct.login_in_progress():
                result = {"ok": False, "msg": "请等待任务或账号登录完成后再更新"}
            else:
                result = UPDATE_DOWNLOADS.install()
                if result["ok"]:
                    threading.Timer(0.4, _local_exit).start()
        elif self.path == "/api/update/open-folder":
            result = UPDATE_DOWNLOADS.open_folder() if WINDOWS else {"ok": False, "msg": "请在发布页查看新版"}
        elif self.path == "/api/browser/preferences":
            result = set_browser_preference(body.get("browser") if isinstance(body, dict) else None)
        elif self.path == "/api/tampermonkey/open":
            result = open_tampermonkey_store()
        elif self.path in {"/api/browser-script/settings", "/api/browser-script/resume"}:
            if not WINDOWS or run_history.participation(DATA_DIR) != "browser":
                result = {"ok": False, "msg": "请先切换到油猴脚本模式"}
            elif self.path.endswith("/resume"):
                if not isinstance(body, dict) or body.get("confirmed") is not True:
                    result = {"ok": False, "msg": "请先确认微软账号状态"}
                else:
                    result = {"ok": True, "scriptAutomation": browser_script.resume(DATA_DIR)}
            elif (not isinstance(body, dict) or type(body.get("openPage")) is not bool
                  or type(body.get("scheduled")) is not bool):
                result = {"ok": False, "msg": "启动方式无效"}
            elif (body["openPage"] or body["scheduled"]) and not risk_acknowledged():
                result = {"ok": False, "msg": "请先确认自动任务的使用风险"}
            else:
                result = {"ok": True, "scriptAutomation": browser_script.configure(DATA_DIR, body["openPage"], body["scheduled"])}
        elif self.path in {"/api/rewards/dashboard", "/api/rewards/redeem"} and not WINDOWS:
            result = {"ok": False, "msg": "请从页面链接打开微软 Rewards"}
        elif self.path in {"/api/rewards/dashboard", "/api/rewards/redeem"}:
            try:
                target = "https://rewards.bing.com/redeem" if self.path.endswith("/redeem") else REWARDS_DASHBOARD
                result = open_selected_browser(target)
            except OSError:
                result = {"ok": False, "msg": "无法打开微软 Rewards 页面，请稍后重试。"}
        elif self.path == "/api/risk/acknowledge":
            if not isinstance(body, dict) or body.get("confirmed") is not True:
                result = {"ok": False, "msg": "请先确认已阅读使用风险"}
            else:
                try:
                    save_prefs({"riskAcknowledged": True})
                    result = {"ok": True}
                except OSError:
                    result = {"ok": False, "msg": "无法保存风险确认，请检查数据目录权限"}
        elif self.path == "/api/participation":
            mode = body.get("mode") if isinstance(body, dict) else None
            if mode == "automatic" and not risk_acknowledged():
                result = {"ok": False, "msg": "请先确认自动任务的使用风险"}
            else:
                with _lock:
                    result = run_history.set_participation(DATA_DIR, mode)
        elif match := re.fullmatch(r"/api/runs/([^/]+)/(login|retry|retry-all)", self.path):
            result = run_account_action(match[1], match[2], body)
        elif self.path == "/api/run":
            only = body.get("only") if isinstance(body, dict) else False
            if only is None or isinstance(only, str):
                if run_history.participation(DATA_DIR) == "browser":
                    run_id = run_history.record_browser_assist(DATA_DIR)
                    result = {"ok": bool(run_id), "runId": run_id,
                              "msg": "" if run_id else "已有记录正在写入，请稍后再试"}
                else:
                    result = start_run(only or None)
            else:
                result = {"ok": False, "msg": "账号参数不合法"}
        elif self.path == "/api/stop":
            result = stop_run()
        elif self.path == "/api/accounts/resume-tasks":
            email = body.get("email") if isinstance(body, dict) else None
            if not isinstance(email, str) or body.get("confirmed") is not True:
                result = {"ok": False, "msg": "请先在微软官网确认账号状态"}
            else:
                with _lock:
                    if run_state() in {"process", "task"}:
                        result = {"ok": False, "msg": "请等待当前运行结束后再恢复账号"}
                    else:
                        result = acct.resume_task_account(email)
        elif self.path == "/api/accounts/login":
            result = acct.start_login(str(body.get("email", "")))
        elif self.path == "/api/accounts/update":
            result = acct.update(str(body.get("email", "")), body.get("fields") or {})
            if result.get("ok"):
                acct.write_env()
        elif self.path == "/api/accounts/switch":
            ok = acct.switch(str(body.get("email", "")))
            if ok:
                acct.write_env()
            result = {"ok": ok}
        elif self.path == "/api/accounts/remove":
            ok = acct.remove(str(body.get("email", "")))
            if ok:
                acct.write_env()
            result = {"ok": ok}
        elif self.path == "/api/accounts/toggle":
            ok = acct.toggle(str(body.get("email", "")), bool(body.get("enabled", True)))
            if ok:
                acct.write_env()
            result = {"ok": ok}
        elif self.path == "/api/config":
            result = set_config(str(body.get("path", "")), body.get("value"))
        elif self.path == "/api/config/batch":
            result = set_config_batch(body.get("items") or {})
        elif self.path == "/api/browser/install":
            result = install_browser()
        elif self.path in ("/api/window/close", "/api/window/open"):
            opening = self.path.endswith("/open")
            action = _local_open if opening else _local_close
            run_id = body.get("runId") if opening and isinstance(body, dict) else None
            if run_id is not None and not valid_run_id(run_id):
                result = {"ok": False, "msg": "运行编号不合法"}
            elif action:
                if run_id is not None:
                    queue_window_run(run_id)
                result = {"ok": True}
                threading.Timer(0.15, action).start()
            else:
                result = {"ok": False, "msg": "当前非桌面模式"}
        elif self.path == "/api/cache/clean":
            try:
                result = clean_cache()
            except BaseException as exc:  # noqa: BLE001
                result = {"ok": False, "msg": f"清理失败: {exc}"}
        elif match := re.fullmatch(r"/api/notifications/(read|clear|preferences)", self.path):
            result = run_history.change_notifications(DATA_DIR, match[1], body.get("muted"))
        elif self.path == "/api/balance/sync":
            result = start_balance_sync()
        elif self.path == "/api/logs/prune":
            result = prune_old_logs()
        elif self.path == "/api/prefs":
            result = set_prefs(body)
        elif self.path == "/api/accounts/bulk":
            result = acct.bulk(str(body.get("action", "")))
            if result.get("ok"):
                acct.write_env()
        elif self.path == "/api/schedule/skip":
            with _schedule_lock:
                result = sched_mod.skip_today()
        elif self.path == "/api/schedule/pause":
            with _schedule_lock:
                result = sched_mod.set_paused(body.get("paused"), body.get("until"))
        elif self.path == "/api/ui":
            _ui_state.clear()
            _ui_state.update(body if isinstance(body, dict) else {})
            result = {"ok": True}
        elif self.path == "/api/schedule":
            with _schedule_lock:
                result = sched_mod.update(str(body.get("mode", "manual")),
                                          body.get("times") or body.get("time"),
                                          body.get("retry"),
                                          body.get("weekdaysOnly"),
                                          body.get("catchUpMode"),
                                          body.get("catchUpMinutes"),
                                          body.get("groups"))
        else:
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")


def seed_config() -> None:
    example = HOME / "config.example.json"
    if CONFIG_FILE.exists() or not example.is_file():
        return
    config = json.loads(example.read_bytes())
    if WINDOWS:
        config["browserChannel"] = ""
    else:
        config["headless"] = True
    defaults = json.dumps(config, indent=4).encode("utf-8")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with CONFIG_FILE.open("xb") as target:
            target.write(defaults)
    except FileExistsError:
        pass


def _schedule_monitor() -> None:
    while True:
        try:
            with _schedule_lock:
                sched_mod.supervise_if_missing()
        except OSError:
            pass
        time.sleep(30)


def main() -> None:
    if not WINDOWS and not TOKEN:
        raise RuntimeError("无法创建面板令牌，请检查数据目录权限")
    seed_config()
    try:
        prune_old_logs()
    except Exception:
        pass
    try:
        purge_old_profiles()
    except Exception:
        pass
    updates.record_upgrade(ROOT, portable_version())
    threading.Thread(target=_schedule_monitor, daemon=True).start()
    print(f"Rewards 面板: http://127.0.0.1:{PORT}")
    print(f"应用根: {ROOT}")
    print(f"数据目录: {DATA_DIR}")
    address = "0.0.0.0" if CONTAINER else "127.0.0.1"
    with ThreadingHTTPServer((address, PORT), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
