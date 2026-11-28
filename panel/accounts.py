#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号库 —— 登录一次记住，可切换，按账号统计，凭据可编辑

字段对齐上游 gui/env_store.py 的 FIELD_ORDER：
    EMAIL / PASSWORD / TOTP_SECRET / RECOVERY_EMAIL / GEO_LOCALE / LANG_CODE
    PROXY_HTTP / PROXY_URL / PROXY_PORT / PROXY_USERNAME / PROXY_PASSWORD
    SAVE_FINGERPRINT_MOBILE / SAVE_FINGERPRINT_DESKTOP

约定：
    * 密钥字段（password / totp / proxy_pass）只在内部用于生成 .env，
      列表接口只返回 has_* 布尔，不回传明文。
    * 写入 .env 前先备份 .env.bak。
    * 只跑某个账号时，用 child_env_for() 把其余 slot 的 EMAIL 置空，
      再把选中的 slot 重映射到 ACCOUNT_1_*（与上游 runner 的做法一致）。
"""
from __future__ import annotations

import base64
import ctypes
from collections import Counter
from functools import lru_cache
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import account_holds
import run_history
from panel import balance

ROOT = Path(os.environ.get("REWARDS_APP_ROOT") or Path(__file__).resolve().parent.parent)
DATA_DIR = Path(os.environ.get("REWARDS_DATA_DIR") or ROOT / "data")
NODE = run_history.node_path(ROOT)
LOGIN_JS = ROOT / "login.js"
BROWSER_ROOT = ROOT / "browser"

ACC_FILE = DATA_DIR / "accounts.json"
ENV_FILE = DATA_DIR / ".env"
ENV_BAK = DATA_DIR / ".env.bak"
SESSION_DB = DATA_DIR / "sessions" / "sessions.db"

SECRET_KEYS = {"password", "totp", "proxy_pass"}


# ---------------------------------------------------------------- 密钥保护（DPAPI）

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> "_BLOB":
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def protect(text: str) -> str:
    """用当前 Windows 用户的 DPAPI 密钥加密，换成别的账户/机器解不开"""
    if not text:
        return ""
    if text.startswith("enc:"):
        return text
    try:
        inb, outb = _blob(text.encode("utf-8")), _BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb))
        if not ok:
            return text
        try:
            raw = ctypes.string_at(outb.pbData, outb.cbData)
            return "enc:" + base64.b64encode(raw).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(outb.pbData)
    except Exception:
        return text


def unprotect(token: str) -> str:
    if not token or not str(token).startswith("enc:"):
        return token or ""
    try:
        raw = base64.b64decode(token[4:])
        inb, outb = _blob(raw), _BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb))
        if not ok:
            return ""
        try:
            return ctypes.string_at(outb.pbData, outb.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(outb.pbData)
    except Exception:
        return ""


RE_LOG_ACCOUNT = re.compile(r"\[([A-Za-z0-9._%+-]{1,20}\*{2,8})\]")
RE_GAIN = re.compile(r"pointsGained=(\d+) \| currentBalance=(\d+)")
RE_BALANCE = re.compile(r"\| (?:currentBalance|当前余额|现余额)=(\d+)(?=\s|$)")

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------- 存取

def _blank(email: str) -> dict:
    return {
        "email": email, "alias": "", "group": "默认", "enabled": True,
        "password": "", "totp": "", "recovery": "", "geo": "auto", "lang": "zh",
        "proxy_http": False, "proxy_url": "", "proxy_port": "",
        "proxy_user": "", "proxy_pass": "",
        "fp_mobile": True, "fp_desktop": True,
        "addedAt": int(time.time() * 1000), "lastLogin": None,
    }


def load() -> dict:
    if ACC_FILE.exists():
        try:
            data = json.loads(ACC_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("accounts"), list):
                for item in data["accounts"]:
                    for k, v in _blank(item.get("email", "")).items():
                        item.setdefault(k, v)
                    for k in SECRET_KEYS:
                        if item.get(k):
                            item[k] = unprotect(str(item[k]))
                # 明文迁移：老文件里的明文密钥，读一次就顺手加密落盘
                if any(acc.get(k) and not str(acc[k]).startswith("enc:")
                       for acc in data["accounts"] for k in SECRET_KEYS):
                    save(data)
                return data
        except Exception:
            pass
    return _migrate_from_env()


def _migrate_from_env() -> dict:
    data = {"active": None, "accounts": []}
    if ENV_FILE.exists():
        found: dict[int, dict] = {}
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            t = line.strip()
            if not t or t.startswith("#") or "=" not in t:
                continue
            k, _, v = t.partition("=")
            m = re.match(r"ACCOUNT_(\d+)_(.+)$", k.strip())
            if not m:
                continue
            found.setdefault(int(m.group(1)), {})[m.group(2)] = v.strip()
        for idx in sorted(found):
            row = found[idx]
            email = row.get("EMAIL")
            if not email:
                continue
            item = _blank(email)
            item["password"] = row.get("PASSWORD", "")
            item["totp"] = row.get("TOTP_SECRET", "")
            item["recovery"] = row.get("RECOVERY_EMAIL", "")
            item["geo"] = row.get("GEO_LOCALE") or "auto"
            item["lang"] = row.get("LANG_CODE") or "zh"
            item["alias"] = email.split("@")[0]
            data["accounts"].append(item)
        if data["accounts"]:
            data["active"] = data["accounts"][0]["email"]
    save(data)
    return data


def save(data: dict) -> None:
    """落盘时把密码类字段 DPAPI 加密，内存里仍是明文"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = {"active": data.get("active"), "accounts": []}
    for acc in data.get("accounts", []):
        row = dict(acc)
        for k in SECRET_KEYS:
            if row.get(k):
                row[k] = protect(str(row[k]))
        out["accounts"].append(row)
    if ACC_FILE.exists():
        try:
            shutil.copy2(ACC_FILE, ACC_FILE.with_suffix(".json.bak"))
        except Exception:
            pass
    ACC_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def _find(data: dict, email: str) -> dict | None:
    for item in data["accounts"]:
        if item["email"].lower() == email.strip().lower():
            return item
    return None


def add(email: str, alias: str = "") -> dict:
    email = email.strip()
    with _lock:
        data = load()
        item = _find(data, email)
        if item:
            if alias:
                item["alias"] = alias
            save(data)
            return item
        item = _blank(email)
        item["alias"] = alias or email.split("@")[0]
        data["accounts"].append(item)
        if not data.get("active"):
            data["active"] = email
        save(data)
        return item


def update(email: str, fields: dict) -> dict:
    """只更新传入的键；空字符串表示清空；密钥字段传 None 表示不改"""
    allowed = {"alias", "group", "enabled", "password", "totp", "recovery", "geo", "lang",
               "proxy_http", "proxy_url", "proxy_port", "proxy_user", "proxy_pass",
               "fp_mobile", "fp_desktop"}
    if "group" in fields and (not isinstance(fields["group"], str) or
                              len(fields["group"]) > 32 or not fields["group"].isprintable()):
        return {"ok": False, "msg": "分组名须为 1 至 32 个可显示字符"}
    with _lock:
        data = load()
        item = _find(data, email)
        if not item:
            return {"ok": False, "msg": "账号不存在"}
        for k, v in (fields or {}).items():
            if k not in allowed:
                continue
            if k in SECRET_KEYS and v is None:
                continue
            if k in ("proxy_http", "fp_mobile", "fp_desktop", "enabled"):
                item[k] = bool(v)
            else:
                item[k] = "" if v is None else str(v)
        save(data)
    return {"ok": True}


def remove(email: str) -> bool:
    with _lock:
        data = load()
        before = len(data["accounts"])
        data["accounts"] = [a for a in data["accounts"] if a["email"].lower() != email.lower()]
        if (data.get("active") or "").lower() == email.lower():
            data["active"] = data["accounts"][0]["email"] if data["accounts"] else None
        save(data)
        return len(data["accounts"]) < before


def bulk(action: str) -> dict:
    """批量操作：enable_all / disable_all / remove_disabled"""
    with _lock:
        data = load()
        changed = 0
        if action == "enable_all":
            for a in data["accounts"]:
                if not a.get("enabled", True):
                    a["enabled"] = True
                    changed += 1
        elif action == "disable_all":
            for a in data["accounts"]:
                if a.get("enabled", True):
                    a["enabled"] = False
                    changed += 1
        elif action == "remove_disabled":
            before = len(data["accounts"])
            data["accounts"] = [a for a in data["accounts"] if a.get("enabled", True)]
            changed = before - len(data["accounts"])
            if data.get("active") and not any(
                    a["email"].lower() == data["active"].lower() for a in data["accounts"]):
                data["active"] = data["accounts"][0]["email"] if data["accounts"] else None
        else:
            return {"ok": False, "msg": "未知操作"}
        save(data)
    return {"ok": True, "changed": changed}


def switch(email: str) -> bool:
    with _lock:
        data = load()
        item = _find(data, email)
        if not item:
            return False
        data["active"] = item["email"]
        save(data)
        return True


def toggle(email: str, enabled: bool) -> bool:
    return update(email, {"enabled": enabled}).get("ok", False)


def mark_login(email: str) -> None:
    with _lock:
        data = load()
        item = _find(data, email)
        if item:
            item["lastLogin"] = int(time.time() * 1000)
            save(data)


# ---------------------------------------------------------------- 生成 .env

def _bool(v) -> str:
    return "true" if v else "false"


def _slot_lines(slot: int, acc: dict) -> list[str]:
    return [
        f"ACCOUNT_{slot}_EMAIL={acc.get('email', '')}",
        f"ACCOUNT_{slot}_PASSWORD={acc.get('password', '')}",
        f"ACCOUNT_{slot}_TOTP_SECRET={acc.get('totp', '')}",
        f"ACCOUNT_{slot}_RECOVERY_EMAIL={acc.get('recovery', '')}",
        f"ACCOUNT_{slot}_GEO_LOCALE={acc.get('geo') or 'auto'}",
        f"ACCOUNT_{slot}_LANG_CODE={acc.get('lang') or 'zh'}",
        f"ACCOUNT_{slot}_PROXY_HTTP={_bool(acc.get('proxy_http'))}",
        f"ACCOUNT_{slot}_PROXY_URL={acc.get('proxy_url', '')}",
        f"ACCOUNT_{slot}_PROXY_PORT={acc.get('proxy_port', '')}",
        f"ACCOUNT_{slot}_PROXY_USERNAME={acc.get('proxy_user', '')}",
        f"ACCOUNT_{slot}_PROXY_PASSWORD={acc.get('proxy_pass', '')}",
        f"ACCOUNT_{slot}_SAVE_FINGERPRINT_MOBILE={_bool(acc.get('fp_mobile'))}",
        f"ACCOUNT_{slot}_SAVE_FINGERPRINT_DESKTOP={_bool(acc.get('fp_desktop'))}",
    ]


def _write_env_text(lines: list[str]) -> None:
    if ENV_FILE.exists():
        try:
            shutil.copy2(ENV_FILE, ENV_BAK)
        except Exception:
            pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_bytes("\r\n".join(lines + [""]).encode("utf-8"))


def write_env() -> dict:
    data = load()
    rows = [a for a in data["accounts"] if a.get("enabled", True)]
    active = (data.get("active") or "").lower()
    rows.sort(key=lambda a: (a["email"].lower() != active, a["email"]))

    lines = ["# 本文件由 Rewards 面板生成，手动改动会被覆盖",
             "# 账号请通过面板「账号」页管理", ""]
    for i, acc in enumerate(rows, start=1):
        lines += _slot_lines(i, acc) + [""]
    _write_env_text(lines)
    return {"accounts": len(rows), "active": data.get("active")}


def child_env_for(email: str) -> dict | None:
    return child_env_for_accounts([email])


def child_env_for_accounts(emails: list[str]) -> dict | None:
    """映射选中的账号并屏蔽环境和 .env 中的其余账号。"""
    with _lock:
        data = load()
        targets = [_find(data, email) for email in emails]
        if not targets or any(target is None or not target.get("enabled", True) for target in targets):
            return None
        env = {key: "" for key in os.environ if re.fullmatch(r"ACCOUNT_[1-9][0-9]*_EMAIL", key, re.IGNORECASE)}
        for path in (ENV_FILE, ROOT / "app/.env", ROOT / "app/dist/.env", ROOT / "app/src/.env"):
            if path.is_file():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    key = line.partition("=")[0].strip()
                    if re.fullmatch(r"ACCOUNT_[1-9][0-9]*_EMAIL", key, re.IGNORECASE):
                        env[key] = ""
                break
        for slot, target in enumerate(targets, start=1):
            env.update(line.split("=", 1) for line in _slot_lines(slot, target))
        return env


def group_emails(group: str) -> list[str]:
    return [a["email"] for a in load()["accounts"]
            if a.get("enabled", True) and (a.get("group") or "默认") == group]


# ---------------------------------------------------------------- 会话与积分

def _session_rows() -> dict:
    out: dict[str, dict] = {}
    if not SESSION_DB.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{SESSION_DB}?mode=ro", uri=True, timeout=2)
        for email, platform, upd in con.execute(
                "SELECT email, platform, updated_at FROM sessions"):
            out.setdefault(email, {})[platform] = upd
        con.close()
    except Exception:
        pass
    return out


def _points_by_mask() -> tuple[dict, int | None]:
    log = ROOT / "app" / "logs" / f"{datetime.now():%Y-%m-%d}.log"
    out: dict[str, int] = {}
    balance = None
    if not log.exists():
        return out, balance
    with open(log, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = RE_LOG_ACCOUNT.search(line)
            g = RE_GAIN.search(line)
            if g:
                balance = int(g.group(2))
                if m:
                    key = m.group(1)
                    out[key] = out.get(key, 0) + int(g.group(1))
    return out, balance


def mask_of(email: str) -> str:
    local = email.split("@")[0]
    return (local[:3] + "****") if len(local) > 3 else (local + "****")


@lru_cache(maxsize=32)
def _logged_balances(path: Path, size: int, modified: int) -> dict:
    rows = {}
    try:
        stream = run_history.open_daily_log(path.with_suffix("")) if path.suffix == ".gz" else (
            path.open(encoding="utf-8", errors="replace")
        )
        with stream:
            for line in stream:
                if "[RUN-END]" in line:
                    continue
                who, points = RE_LOG_ACCOUNT.search(line), RE_BALANCE.search(line)
                if who and points:
                    try:
                        observed = datetime.fromisoformat(line[:23]).timestamp()
                    except ValueError:
                        observed = modified / 1_000_000_000
                    rows[who[1].casefold()] = (int(points[1]), observed)
    except (OSError, EOFError):
        pass
    return rows


def account_balances(emails: list[str]) -> dict:
    try:
        recorded = balance.recent(DATA_DIR)
    except sqlite3.Error:
        recorded = {}
    counts = Counter(mask_of(email).casefold() for email in emails)
    pending = {mask_of(email).casefold(): email.casefold() for email in emails if counts[mask_of(email).casefold()] == 1}
    logs = ROOT / "app" / "logs"
    for path in sorted(logs.glob("????-??-??.log*"), reverse=True):
        if not pending:
            break
        if path.suffix not in (".log", ".gz"):
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        for mask, (points, observed) in _logged_balances(path, info.st_size, info.st_mtime_ns).items():
            email = pending.pop(mask, None)
            if email and observed > recorded.get(email, {}).get("at", 0):
                recorded[email] = {"balance": points, "at": observed, "source": "log"}
    return recorded


def summary() -> dict:
    data = load()
    sessions = _session_rows()
    login_failures = run_history.account_login_failures(DATA_DIR)
    points, balance = _points_by_mask()
    balances = account_balances([acc["email"] for acc in data["accounts"]])
    now = time.time() * 1000
    items = []
    for acc in data["accounts"]:
        row = sessions.get(acc["email"], {})
        upd = row.get("desktop") or row.get("mobile")
        last_login = acc.get("lastLogin")
        last_verified = last_login if type(last_login) is int else 0
        needs_login = login_failures.get(acc["email"].casefold(), 0) > last_verified
        latest = balances.get(acc["email"].casefold(), {})
        hold = account_holds.get(DATA_DIR, acc["email"])
        items.append({
            "email": acc["email"],
            "alias": acc.get("alias") or acc["email"].split("@")[0],
            "group": acc.get("group") or "默认",
            "enabled": acc.get("enabled", True),
            "active": (data.get("active") or "").lower() == acc["email"].lower(),
            "loggedIn": bool(upd) and not needs_login,
            "needsLogin": needs_login,
            "taskHold": hold["reason"] if hold else None,
            "sessionHours": round((now - upd) / 3600000, 1) if upd else None,
            "points": points.get(mask_of(acc["email"]), 0),
            "balance": latest.get("balance"),
            "balanceSource": latest.get("source"),
            "balanceAt": latest.get("at"),
            "hasTotp": bool(acc.get("totp")),
            "hasPassword": bool(acc.get("password")),
            "hasRecovery": bool(acc.get("recovery")),
            "hasProxy": bool(acc.get("proxy_url")),
            "proxyUrl": acc.get("proxy_url", ""),
            "proxyPort": acc.get("proxy_port", ""),
            "hasProxyPass": bool(acc.get("proxy_pass")),
            "geo": acc.get("geo") or "auto",
            "lang": acc.get("lang") or "zh",
            "fpMobile": acc.get("fp_mobile", True),
            "fpDesktop": acc.get("fp_desktop", True),
            "lastLogin": acc.get("lastLogin"),
        })
    items.sort(key=lambda x: (not x["active"], not x["enabled"], x["email"]))
    return {"active": data.get("active"), "items": items,
            "totalPoints": sum(i["points"] for i in items), "balance": balance}


# ---------------------------------------------------------------- 登录任务

def resume_task_account(email: str) -> dict:
    with _lock:
        if not _find(load(), email):
            return {"ok": False, "msg": "找不到该账号"}
        try:
            account_holds.resume(DATA_DIR, email)
        except OSError:
            return {"ok": False, "msg": "无法清除账号暂停状态，请检查数据目录权限"}
        return {"ok": True}


def login_in_progress() -> bool:
    with _lock:
        return any(job["state"] == "running" for job in _jobs.values())


def start_login(email: str, *, existing_only: bool = False) -> dict:
    email = email.strip()
    if not email or "@" not in email:
        return {"ok": False, "msg": "邮箱格式不对"}
    if NODE is None or not NODE.is_file() or not LOGIN_JS.is_file():
        return {"ok": False, "msg": "缺少 Node.js 或 login.js"}
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        return {"ok": False, "msg": "没有可用的图形会话。请在 Linux 桌面启动面板，或按 Docker 登录说明连接 VNC。"}
    with _lock:
        if account_holds.get(DATA_DIR, email):
            return {"ok": False, "msg": "账号已暂停，请到微软官网确认后手动恢复"}
        if existing_only:
            account = _find(load(), email)
            if account is None:
                return {"ok": False, "msg": "此账号已移除"}
            email = account["email"]
        for job in _jobs.values():
            if job.get("state") == "running":
                return {"ok": False, "msg": "已有登录窗口在跑，先完成或关掉它"}
        job_id = str(int(time.time() * 1000))
        _jobs[job_id] = {"state": "running", "email": email,
                         "since": time.time(), "cookies": 0, "msg": "等待登录"}
    if not existing_only:
        add(email)

    def worker() -> None:
        env = dict(os.environ)
        env["REWARDS_APP_ROOT"] = str(ROOT)
        env["REWARDS_DATA_DIR"] = str(DATA_DIR)
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_ROOT)
        env["REWARDS_LOGIN_BROWSER"] = run_history.engine_browser_channel(DATA_DIR)
        try:
            with TemporaryDirectory(prefix="login-profile.", dir=DATA_DIR, ignore_cleanup_errors=True) as profile:
                capture_login(env, profile)
        except OSError:
            _jobs[job_id].update(state="failed", msg="无法准备登录窗口")


    def capture_login(env: dict, profile: str) -> None:
        try:
            proc = subprocess.Popen(
                [str(NODE), str(LOGIN_JS), "--email", email, "--profile", profile],
                cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError:
            _jobs[job_id].update(state="failed", msg="无法打开登录窗口")
            return
        assert proc.stdout is not None
        saved_cookies = None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if payload.get("stage") == "saved":
                try:
                    mark_login(email)
                except OSError:
                    _jobs[job_id].update(state="failed", msg="已保存会话，但无法更新账号登录状态")
                    continue
                saved_cookies = payload.get("cookies", 0)
            elif payload.get("ok") is False:
                _jobs[job_id].update(state="failed", msg=payload.get("error", "失败"))
        proc.wait()
        if saved_cookies is not None:
            _jobs[job_id].update(state="saved", cookies=saved_cookies, msg="登录成功")
        elif _jobs[job_id]["state"] == "running":
            _jobs[job_id].update(state="failed", msg="窗口已关闭或超时")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "job": job_id}


def job_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {"state": "unknown"}
    return {k: (round(time.time() - v) if k == "since" else v) for k, v in job.items()}
