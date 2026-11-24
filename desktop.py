#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rewards 桌面外壳（无边框窗口）

职责：
    1. 注入环境变量，按需拉起后端（8765）
    2. 单实例：已有窗口则激活它并退出
    3. 记住窗口位置尺寸（data/prefs.json 的 win）
    4. 关窗即退出，退出前把在跑的任务停掉
    5. 设置窗口 / 任务栏图标

无边框拖动靠 pywebview 的 easy_drag（按住页面空白处拖）。
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["REWARDS_APP_ROOT"] = str(ROOT)
os.environ.setdefault("REWARDS_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "browser"))

PORT = int(os.environ.get("REWARDS_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
TITLE = "Rewards"
DEFAULT_SIZE = (1320, 900)
MIN_SIZE = (1040, 700)
ICO = ROOT / "assets" / "icon.ico"
PNG = ROOT / "assets" / "icon.png"
PREFS = ROOT / "data" / "prefs.json"
TOKEN_FILE = ROOT / "data" / ".api-token"

STATE = {"quitting": False}


# ---------------------------------------------------------------- 基础设施

def _log(msg: str) -> None:
    try:
        (ROOT / "logs").mkdir(exist_ok=True)
        with open(ROOT / "logs" / "desktop.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_backend() -> None:
    if port_open(PORT):
        return
    sys.path.insert(0, str(ROOT / "panel"))
    import server  # noqa: PLC0415

    threading.Thread(target=server.main, daemon=True).start()
    for _ in range(80):
        if port_open(PORT):
            return
        time.sleep(0.15)


def token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def page_url() -> str:
    """带令牌的入口地址，首次访问会把 cookie 种下来"""
    t = token()
    return f"{URL}/?k={t}" if t else URL


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    t = token()
    url = f"{URL}{path}{'&' if '?' in path else '?'}k={t}" if t else f"{URL}{path}"
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": str(exc)}


def task_running() -> bool:
    return bool(api("/api/status").get("running"))


AUMID = "Rewards.Desktop.App"
SHORTCUT = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Rewards.lnk"


def ensure_shortcut() -> None:
    """在开始菜单建一个带 AUMID 的快捷方式

    任务栏右键里显示 "Python"，是因为进程可执行文件是 pythonw.exe，
    Windows 只能读它自己的文件描述。给同名 AUMID 配一个快捷方式后，
    系统就能解析出应用名和图标。
    """
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell
    except Exception as exc:  # noqa: BLE001
        _log(f"未安装 pywin32，跳过快捷方式: {exc}")
        return

    try:
        exe = Path(sys.executable)
        if exe.name.lower() == "python.exe":
            cand = exe.with_name("pythonw.exe")
            if cand.exists():
                exe = cand

        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                          pythoncom.CLSCTX_INPROC_SERVER,
                                          shell.IID_IShellLink)
        link.SetPath(str(exe))
        link.SetArguments(f'"{ROOT / "desktop.py"}"')
        link.SetWorkingDirectory(str(ROOT))
        link.SetIconLocation(str(ICO), 0)
        link.SetDescription("Rewards")
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(SHORTCUT), 0)

        store = propsys.SHGetPropertyStoreFromParsingName(str(SHORTCUT), None, 2)
        store.SetValue(pscon.PKEY_AppUserModel_ID,
                       propsys.PROPVARIANTType(AUMID, pythoncom.VT_LPWSTR))
        store.Commit()
        del store
        _log(f"快捷方式与 AUMID 就绪: {SHORTCUT}")
    except Exception as exc:  # noqa: BLE001
        _log(f"快捷方式处理失败: {exc}")


# ---------------------------------------------------------------- 单实例

def activate_existing() -> bool:
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


# ---------------------------------------------------------------- 窗口几何

def load_geometry() -> tuple[int, int, int, int]:
    w, h = DEFAULT_SIZE
    x, y = -1, -1
    try:
        data = json.loads(PREFS.read_text(encoding="utf-8"))
        win = data.get("win") or {}
        w = int(win.get("w") or w)
        h = int(win.get("h") or h)
        x = int(win.get("x", -1))
        y = int(win.get("y", -1))
    except Exception:
        pass
    w = max(MIN_SIZE[0], min(w, 4000))
    h = max(MIN_SIZE[1], min(h, 3000))
    return x, y, w, h


def win_delta() -> tuple[int, int]:
    """客户区与外框的固定差值（无边框窗口也有 15/38 左右的差值）"""
    try:
        d = json.loads(PREFS.read_text(encoding="utf-8"))
        f = d.get("winDelta") or {}
        return int(f.get("w", 0)), int(f.get("h", 0))
    except Exception:
        return 0, 0


def learn_delta(window, requested: tuple[int, int]) -> None:
    """开窗后量一次差多少，存起来供下次补偿"""
    try:
        dw = int(requested[0]) - int(window.width)
        dh = int(requested[1]) - int(window.height)
        if not (0 <= dw < 200 and 0 <= dh < 300):
            return
        data = json.loads(PREFS.read_text(encoding="utf-8")) if PREFS.exists() else {}
        if data.get("winDelta") != {"w": dw, "h": dh}:
            data["winDelta"] = {"w": dw, "h": dh}
            PREFS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            _log(f"窗口边框补偿: w+{dw} h+{dh}")
    except Exception:
        pass


def save_geometry(window, force: bool = False) -> None:
    """写窗口几何。尺寸变化小于 40px 就不写，避免每次启动被工作区裁一点、越开越小"""
    try:
        data = {}
        if PREFS.exists():
            data = json.loads(PREFS.read_text(encoding="utf-8")) or {}
        dw, dh = win_delta()
        cur = {"x": int(window.x), "y": int(window.y),
               "w": int(window.width) + dw, "h": int(window.height) + dh}
        old = data.get("win") or {}
        if not force and old:
            same_pos = abs(cur["x"] - int(old.get("x", -999))) <= 2 and abs(cur["y"] - int(old.get("y", -999))) <= 2
            close_size = abs(cur["w"] - int(old.get("w", 0))) < 40 and abs(cur["h"] - int(old.get("h", 0))) < 40
            if same_pos and close_size:
                return
        data["win"] = cur
        PREFS.parent.mkdir(parents=True, exist_ok=True)
        PREFS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _log(f"保存窗口尺寸失败: {exc}")


# ---------------------------------------------------------------- 图标

def apply_icon() -> bool:
    if not ICO.exists():
        _log("icon.ico 不存在")
        return False
    user32 = ctypes.windll.user32
    hwnd = 0
    for _ in range(40):
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            break
        time.sleep(0.15)
    if not hwnd:
        _log("找不到窗口句柄，图标未设置")
        return False
    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x0080
    ok = 0
    for size, which in ((32, 1), (16, 0)):
        handle = user32.LoadImageW(None, str(ICO), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if handle:
            user32.SendMessageW(hwnd, WM_SETICON, which, handle)
            ok += 1
    _log(f"窗口图标已设置 hwnd={hwnd} 尺寸数={ok}")
    return ok > 0


# ---------------------------------------------------------------- 页面 API

class Api:
    """暴露给页面的窗口控制（属性一律下划线开头，避免被序列化递归）"""

    def __init__(self) -> None:
        self._win = None
        self._maxed = False
        self._on_close = None

    def attach(self, window, on_close=None) -> None:
        self._win = window
        self._on_close = on_close

    def minimize(self) -> bool:
        if self._win:
            self._win.minimize()
            return True
        return False

    def toggle_max(self) -> bool:
        if not self._win:
            return False
        try:
            if self._maxed:
                self._win.restore()
            else:
                self._win.maximize()
            self._maxed = not self._maxed
        except Exception:
            return self._maxed
        return self._maxed

    def close(self) -> bool:
        """页面上的 ✕：先清理（停任务、存几何），再真正销毁窗口"""
        try:
            if self._on_close:
                self._on_close()        # 只做清理，返回值是给原生事件用的，这里不用
        except Exception:
            pass
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
            return True
        return False


# ---------------------------------------------------------------- 启动

def fallback_chromium() -> int:
    found = sorted((ROOT / "browser").glob("chromium-*/chrome-win64/chrome.exe"))
    if not found:
        print("找不到内置浏览器，也无法使用 pywebview")
        return 1
    x, y, w, h = load_geometry()
    args = [str(found[-1]), f"--app={page_url()}",
            f"--user-data-dir={ROOT / 'ui-profile'}",
            f"--window-size={w},{h}", "--no-first-run", "--no-default-browser-check"]
    if x >= 0 and y >= 0:
        args.append(f"--window-position={x},{y}")
    subprocess.Popen(args)
    return 0


def main() -> int:
    if activate_existing():
        _log("已有实例在运行，已激活并退出本次启动")
        return 0

    start_backend()

    try:
        import webview  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"pywebview 不可用（{exc}），改用内置浏览器窗口")
        return fallback_chromium()

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    except Exception:
        pass
    ensure_shortcut()

    x, y, w, h = load_geometry()
    api_obj = Api()
    win_kwargs = dict(width=w, height=h, min_size=MIN_SIZE, background_color="#f4f6fa",
                      frameless=True, easy_drag=True, js_api=api_obj)
    if x >= 0 and y >= 0:
        win_kwargs.update(x=x, y=y)

    try:
        window = webview.create_window(TITLE, page_url(), **win_kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"WebView2 启动失败（{exc}），改用内置浏览器窗口")
        return fallback_chromium()

    def on_closing():
        """关窗即退出；任务在跑就先停掉，避免留下看不见的孤儿进程"""
        if STATE["quitting"]:
            return True
        STATE["quitting"] = True
        try:
            if task_running():
                api("/api/stop", "POST", {})
        except Exception:
            pass
        save_geometry(window)
        return True

    def geometry_keeper():
        """每 3 秒比对一次窗口位置尺寸，变化就写盘（不依赖关闭事件）"""
        last = None
        while True:
            time.sleep(3)
            try:
                if getattr(window, "maximized", False) or getattr(window, "fullscreen", False):
                    continue
                cur = (int(window.x), int(window.y), int(window.width), int(window.height))
                if cur != last:
                    last = cur
                    save_geometry(window)
            except Exception:
                return

    threading.Timer(1.5, lambda: learn_delta(window, (w, h))).start()
    threading.Thread(target=geometry_keeper, daemon=True).start()
    api_obj.attach(window, on_close=on_closing)
    try:
        import server as _srv  # noqa: PLC0415
        _srv._local_close = api_obj.close
    except Exception:
        pass
    threading.Thread(target=apply_icon, daemon=True).start()

    try:
        window.events.closing += on_closing
        _log("关闭事件已注册")
    except Exception as exc:  # noqa: BLE001
        _log(f"关闭事件注册失败: {exc}")

    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
