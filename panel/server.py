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

import ctypes
import json
import os
import secrets
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import accounts as acct
import schedule as sched_mod

ROOT = Path(os.environ.get("REWARDS_APP_ROOT") or Path(__file__).resolve().parent.parent)
HOME = ROOT / "app"
NODE = HOME / "node.exe"
ENTRY = HOME / "dist" / "index.js"
LOG_DIR = HOME / "logs"
BROWSER_ROOT = ROOT / "browser"


def _data_dir() -> Path:
    env = os.environ.get("REWARDS_DATA_DIR")
    if env:
        return Path(env)
    local = ROOT / "data"
    if local.exists():
        return local
    return Path(os.environ.get("LOCALAPPDATA", "")) / "RewardsBot"


DATA_DIR = _data_dir()
SESSION_DB = DATA_DIR / "sessions" / "sessions.db"
CONFIG_FILE = DATA_DIR / "config.json"
PREFS_FILE = DATA_DIR / "prefs.json"
PREFS_DEFAULT = {"theme": "light", "onlyActive": False}
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


def prune_old_logs(keep_days: int = 30) -> dict:
    """删除 app/logs 下超过保留期的日志，并给调度日志封顶"""
    import time as _t
    removed, freed = 0, 0
    cutoff = _t.time() - keep_days * 86400
    logdir = HOME / "logs"
    if logdir.exists():
        for f in logdir.glob("*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    freed += f.stat().st_size
                    f.unlink()
                    removed += 1
            except Exception:
                pass
    sl = ROOT / "logs" / "scheduler.log"
    try:
        if sl.exists() and sl.stat().st_size > 2 * 1024 * 1024:
            lines = sl.read_text(encoding="utf-8", errors="ignore").splitlines()[-800:]
            sl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
    return {"removed": removed, "freed": freed}


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


def get_prefs() -> dict:
    out = dict(PREFS_DEFAULT)
    try:
        got = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        if isinstance(got, dict):
            out.update({k: got[k] for k in PREFS_DEFAULT if k in got})
    except Exception:
        pass
    return out


def set_prefs(patch: dict) -> dict:
    if not isinstance(patch, dict):
        return {"ok": False, "msg": "参数不对"}
    cur = get_prefs()
    for k in PREFS_DEFAULT:
        if k in patch:
            cur[k] = patch[k]
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": str(exc)}
    return {"ok": True, "prefs": cur}


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
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for path, value in items.items():
            node = data
            parts = path.split(".")
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        return {"ok": True, "changed": len(items)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": str(exc)}


def install_browser() -> dict:
    node = HOME / "node.exe"
    cli = HOME / "node_modules" / "patchright" / "cli.js"
    if not node.exists() or not cli.exists():
        return {"ok": False, "msg": "找不到 node.exe 或 patchright/cli.js"}
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_ROOT)
    (ROOT / "logs").mkdir(exist_ok=True)
    fh = open(ROOT / "logs" / "browser-install.log", "ab")
    try:
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


def read_logs(count: int = 300) -> dict:
    path = HOME / "logs" / f"{datetime.now():%Y-%m-%d}.log"
    if not path.exists():
        return {"lines": [], "path": str(path), "count": 0}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            tail = fh.readlines()[-max(20, min(count, 2000)):]
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
    node = HOME / "node.exe"
    if not node.exists():
        return False, "缺少 node.exe"
    try:
        res = subprocess.run([str(node), "-v"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (res.stdout or res.stderr).strip()
        return res.returncode == 0, out or "无输出"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:80]


def system_browsers() -> dict:
    def probe(candidates):
        for c in candidates:
            if c and Path(c).exists():
                return str(c)
        return ""

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    chrome = probe([str(Path(pf) / "Google/Chrome/Application/chrome.exe"),
                    str(Path(pf86) / "Google/Chrome/Application/chrome.exe"),
                    str(Path(local) / "Google/Chrome/Application/chrome.exe")])
    edge = probe([str(Path(pf86) / "Microsoft/Edge/Application/msedge.exe"),
                  str(Path(pf) / "Microsoft/Edge/Application/msedge.exe")])
    return {"chrome": chrome, "edge": edge}


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
    checks.append({"name": "定时守护依赖", "ok": bool(pyw), "detail": pyw or "未找到 pythonw.exe"})

    sysb = system_browsers()
    checks.append({"name": "系统浏览器", "ok": True,
                   "detail": "Chrome " + ("有" if sysb["chrome"] else "无")
                             + " / Edge " + ("有" if sysb["edge"] else "无")})

    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "version": _app_version(), "dataDir": str(DATA_DIR),
            "appDir": str(HOME), "root": str(ROOT)}


def _app_version() -> str:
    try:
        pkg = json.loads((HOME / "package.json").read_text(encoding="utf-8"))
        return str(pkg.get("version", ""))
    except Exception:
        return ""

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_cache = {"key": None, "data": None}
_ui_state: dict = {}
_local_close = None   # 由桌面外壳注入，用于关闭窗口
_stop_mark: float = 0.0   # 上次手动停止时日志的 mtime，用来压掉"刚跑完仍显示运行中"

RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+ ")
RE_TITLE = re.compile(r'标题="([^"]*)"')
RE_GAIN = re.compile(r"pointsGained=(\d+) \| currentBalance=(\d+)")
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
        return {"actions": [], "balance": None, "startBalance": None, "gained": 0,
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


def build_status() -> dict:
    state = run_state()
    return {
        "running": state is not None,
        "runningBy": state,
        "orphan": (state == "task" and not proc_alive()),
        "pid": _proc.pid if proc_alive() else None,
        "now": datetime.now().strftime("%H:%M:%S"),
        "log": _slim_log(),
        "session": session_info(),
        "env": env_summary(),
        "accounts": acct.summary(),
        "schedule": sched_mod.status(),
        "ui": dict(_ui_state),
        "cacheBytes": cache_size(),
        "app": {
            "home": str(HOME),
            "root": str(ROOT),
            "nodeOk": NODE.exists(),
            "entryOk": ENTRY.exists(),
            "browser": browser_version(),
        },
    }


def start_run(only: str | None = None) -> dict:
    global _proc
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return {"ok": False, "msg": "已经在运行中"}
        # 定时守护可能已经拉起任务，避免同时跑两份抢同一个账号
        if run_state() is not None:
            return {"ok": False, "msg": "已有任务在运行（可能由定时任务启动）"}
        if not NODE.exists() or not ENTRY.exists():
            return {"ok": False, "msg": f"找不到 {NODE} 或 {ENTRY}"}
        _cache["key"] = None
        env = dict(os.environ)
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_ROOT)
        env["REWARDS_DATA_DIR"] = str(DATA_DIR)
        env["REWARDS_APP_ROOT"] = str(ROOT)
        env["NODE_NO_WARNINGS"] = "1"
        if only:
            # 只跑一个账号：靠环境变量覆盖，不动 .env
            env.update(acct.child_env_for(only))
        else:
            try:
                acct.write_env()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "msg": f"生成 .env 失败: {exc}"}
        _proc = subprocess.Popen(
            [str(NODE), str(ENTRY)],
            cwd=str(HOME),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            (DATA_DIR / "task.pid").write_text(str(_proc.pid), encoding="utf-8")
        except Exception:
            pass
        return {"ok": True, "pid": _proc.pid}


def stop_run() -> dict:
    global _proc
    with _lock:
        # 定时守护启动的任务不在 _proc 里，兜底按 task.pid 杀
        if _proc is None or _proc.poll() is not None:
            ext = task_pid()
            if ext and _pid_alive(ext):
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(ext)],
                                   capture_output=True, timeout=8)
                except Exception:
                    return {"ok": False, "msg": "结束进程失败"}
                try:
                    (DATA_DIR / "task.pid").unlink()
                except Exception:
                    pass
                mark_stopped()
                return {"ok": True, "stopped": ext}
            return {"ok": False, "msg": "当前没有在跑"}
        pid = _proc.pid
        # 先不带 /F，让 node 有机会走完当前写入；10 秒不退再强杀
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                           capture_output=True, timeout=6,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
        for _ in range(20):
            if _proc.poll() is not None:
                break
            time.sleep(0.5)
        else:
            _proc.kill()
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=8)
        except Exception:
            pass
        mark_stopped()
        return {"ok": True, "stopped": pid}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_set_token", False) and TOKEN:
            self.send_header("Set-Cookie", f"rw_token={TOKEN}; Path=/; SameSite=Lax")
            self._set_token = False
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """无令牌文件时全部放行；有令牌则要求 cookie 或首次 ?k="""
        if not TOKEN:
            return True
        if "rw_token=" + TOKEN in (self.headers.get("Cookie") or ""):
            return True
        try:
            from urllib.parse import urlparse, parse_qs
            if parse_qs(urlparse(self.path).query).get("k", [""])[0] == TOKEN:
                self._set_token = True      # 首次带 ?k= 进来，顺手写入 cookie
                return True
        except Exception:
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
        if self.path.startswith("/api/status"):
            self._send(200, json.dumps(build_status(), ensure_ascii=False).encode("utf-8"),
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
        if self.path.startswith("/api/logs"):
            n = 300
            if "lines=" in self.path:
                try:
                    n = int(self.path.split("lines=", 1)[1].split("&")[0])
                except Exception:
                    n = 300
            self._send(200, json.dumps(read_logs(n), ensure_ascii=False).encode("utf-8"),
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
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
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

    def _route_post(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

        if self.path == "/api/run":
            result = start_run(body.get("only") or None)
        elif self.path == "/api/stop":
            result = stop_run()
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
        elif self.path == "/api/window/close":
            if _local_close:
                result = {"ok": True}
                threading.Timer(0.15, _local_close).start()
            else:
                result = {"ok": False, "msg": "当前非桌面模式"}
        elif self.path == "/api/cache/clean":
            try:
                result = clean_cache()
            except BaseException as exc:  # noqa: BLE001
                result = {"ok": False, "msg": f"清理失败: {exc}"}
        elif self.path == "/api/logs/prune":
            result = prune_old_logs()
        elif self.path == "/api/prefs":
            result = set_prefs(body)
        elif self.path == "/api/accounts/bulk":
            result = acct.bulk(str(body.get("action", "")))
            if result.get("ok"):
                acct.write_env()
        elif self.path == "/api/schedule/skip":
            result = sched_mod.skip_today()
        elif self.path == "/api/ui":
            _ui_state.clear()
            _ui_state.update(body if isinstance(body, dict) else {})
            result = {"ok": True}
        elif self.path == "/api/schedule":
            result = sched_mod.update(str(body.get("mode", "manual")),
                                      body.get("times") or body.get("time"),
                                      body.get("retry"),
                                      body.get("weekdaysOnly"))
        else:
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")


def seed_config() -> None:
    example = HOME / "config.example.json"
    if CONFIG_FILE.exists() or not example.is_file():
        return
    defaults = example.read_bytes()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with CONFIG_FILE.open("xb") as target:
            target.write(defaults)
    except FileExistsError:
        pass


def main() -> None:
    seed_config()
    try:
        prune_old_logs()
    except Exception:
        pass
    try:
        purge_old_profiles()
    except Exception:
        pass
    print(f"Rewards 面板: http://127.0.0.1:{PORT}")
    print(f"应用根: {ROOT}")
    print(f"数据目录: {DATA_DIR}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
