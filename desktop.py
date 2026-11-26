#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rewards 桌面外壳（无边框窗口）

职责：
    1. 注入环境变量，按需拉起后端（8765）
    2. 单实例：已有窗口则激活它并退出
    3. 记住窗口位置尺寸（data/prefs.json 的 win）
    4. 按设置关窗退出或保留在系统托盘
    5. 设置窗口 / 任务栏图标

拖动只发生在顶栏（页面自己派的 begin_drag 请求），页面其它地方不响应。
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
# The portable Python's ._pth file excludes the script directory.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from windows_notifications import AUMID, parse_activation, register_shortcut, run_scheme

ACTIVATION = parse_activation(sys.argv[1:]) if __name__ == "__main__" and len(sys.argv) > 1 else None
if __name__ == "__main__" and len(sys.argv) > 1 and ACTIVATION is None:
    raise SystemExit(2)
if ACTIVATION is not None:
    directory, port, _ = ACTIVATION
    os.environ["REWARDS_DATA_DIR"] = str(directory)
    os.environ["REWARDS_PORT"] = str(port)

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
DATA = Path(os.environ["REWARDS_DATA_DIR"]).resolve()
PREFS = DATA / "prefs.json"
TOKEN_FILE = DATA / ".api-token"

STATE = {"quitting": False, "hidden": False}


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


def start_backend(run_id: int | None = None) -> None:
    sys.path.insert(0, str(ROOT / "panel"))
    if port_open(PORT):
        return
    import server  # noqa: PLC0415

    if run_id is not None:
        server.queue_window_run(run_id)
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
    url = f"{URL}/?desktop=1"
    return f"{url}&k={t}" if t else url


def api(path: str, method: str = "GET", body: dict | None = None, *, timeout: float = 8) -> dict:
    t = token()
    url = f"{URL}{path}{'&' if '?' in path else '?'}k={t}" if t else f"{URL}{path}"
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": str(exc)}


def task_running() -> bool:
    return bool(api("/api/status").get("running"))


def stop_task() -> None:
    if task_running():
        api("/api/stop", "POST", {})


def keep_in_tray() -> bool:
    return api("/api/prefs", timeout=2).get("keepInTray") is True


def bind_window_actions(open_panel, close_panel) -> None:
    import server

    server._local_open = open_panel
    server._local_close = close_panel


def make_tray(open_panel, open_pause_picker, exit_app):
    try:
        from windows_tray import Tray
    except ImportError as exc:
        _log(f"托盘不可用: {exc}")
        return None

    def pause_schedule(paused: bool, until: str | None = None) -> None:
        result = api("/api/schedule/pause", "POST", {"paused": paused, "until": until})
        if not result.get("ok"):
            msg = result.get("msg") or "无法更新定时暂停状态"
            _log(msg)
            ctypes.windll.user32.MessageBoxW(0, msg, TITLE, 0x30)

    return Tray(ICO, open_panel, lambda: api("/api/schedule", timeout=2),
                pause_schedule, open_pause_picker, exit_app, _log)


def ensure_shortcut() -> None:
    """在开始菜单建一个带 AUMID 的快捷方式

    任务栏右键里显示 "Python"，是因为进程可执行文件是 pythonw.exe，
    Windows 只能读它自己的文件描述。给同名 AUMID 配一个快捷方式后，
    系统就能解析出应用名和图标。
    """
    try:
        shortcut = register_shortcut(ROOT, refresh=True)
        _log(f"快捷方式与 AUMID 就绪: {shortcut}")
    except Exception as exc:  # noqa: BLE001
        _log(f"快捷方式处理失败: {exc}")


# ---------------------------------------------------------------- 单实例

def activate_existing(run_id: int | None = None, *, wait: bool = False) -> bool:
    body = {"runId": run_id} if run_id is not None else {}
    deadline = time.monotonic() + 12
    while True:
        if port_open(PORT) and api("/api/window/open", "POST", body, timeout=2).get("ok"):
            return True
        if not wait or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if run_id is not None:
        return False
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return False
    foreground(hwnd)
    return True


def foreground(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
    user32.SetForegroundWindow(hwnd)


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
            import server
            server.save_prefs({"winDelta": {"w": dw, "h": dh}})
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
        import server
        server.save_prefs({"win": cur})
    except Exception as exc:  # noqa: BLE001
        _log(f"保存窗口尺寸失败: {exc}")




class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_dragging = threading.Lock()


def _drag_loop() -> None:
    """按住期间每 8ms 把窗口挪到“光标位移”对应的位置

    不走 JS 桥逐帧通信，所以拖动是跟手的。
    """
    if not _dragging.acquire(blocking=False):
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, TITLE)
        if not hwnd:
            return
        start = _POINT()
        user32.GetCursorPos(ctypes.byref(start))
        rect = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        base_x, base_y = rect.left, rect.top

        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
        VK_LBUTTON = 0x01
        while user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            cur = _POINT()
            user32.GetCursorPos(ctypes.byref(cur))
            user32.SetWindowPos(hwnd, None,
                                base_x + (cur.x - start.x),
                                base_y + (cur.y - start.y),
                                0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.008)
    finally:
        _dragging.release()

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

    def attach(self, window) -> None:
        self._win = window

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

    def drag(self) -> bool:
        """页面顶栏按下时调用，开始拖动窗口"""
        if not self._win or self._maxed:
            return False
        threading.Thread(target=_drag_loop, daemon=True).start()
        return True

    def close(self) -> bool:
        """页面的关闭按钮走同一个原生关窗事件。"""
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
            return True
        return False


# ---------------------------------------------------------------- 启动

def bundled_webview_runtime() -> Path | None:
    runtime = ROOT / "runtime" / "webview2"
    if not (runtime / "msedgewebview2.exe").is_file():
        return None
    if sys.getwindowsversion().build < 22000:
        # Fixed WebView2 on Windows 10 gives its renderer an AppContainer token.
        icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
        subprocess.run(
            [str(icacls), str(runtime), "/grant",
             "*S-1-15-2-2:(OI)(CI)(RX)", "*S-1-15-2-1:(OI)(CI)(RX)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
        )
    return runtime


def fallback_chromium() -> int:
    found = sorted((ROOT / "browser").glob("chromium-*/chrome-win64/chrome.exe"))
    if not found:
        print("找不到内置浏览器，也无法使用 pywebview")
        return 1
    x, y, w, h = load_geometry()
    args = [str(found[-1]), f"--app={page_url()}",
            f"--user-data-dir={ROOT / 'ui-profile'}",
            f"--window-size={w},{h}", "--no-first-run", "--no-default-browser-check", "--disable-background-mode"]
    if x >= 0 and y >= 0:
        args.append(f"--window-position={x},{y}")
    wake = threading.Event()
    browser = None
    pause_picker = False

    def browser_window() -> int:
        if not browser or browser.poll() is not None:
            return 0
        try:
            import win32gui
            import win32process
        except ImportError:
            return 0
        found_windows = []

        def collect(hwnd, _):
            if (win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "Chrome_WidgetWin_1"
                    and win32process.GetWindowThreadProcessId(hwnd)[1] == browser.pid):
                found_windows.append(hwnd)

        win32gui.EnumWindows(collect, None)
        return next(iter(found_windows), 0)

    def open_panel() -> None:
        hwnd = browser_window()
        if hwnd:
            foreground(hwnd)
        elif browser and browser.poll() is not None:
            wake.set()

    def open_pause_picker() -> None:
        nonlocal pause_picker
        pause_picker = True
        open_panel()

    def close_panel() -> None:
        if hwnd := browser_window():
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def exit_app() -> None:
        STATE["quitting"] = True
        wake.set()

    tray = make_tray(open_panel, open_pause_picker, exit_app)
    bind_window_actions(open_panel, close_panel)
    code = 0
    try:
        while not STATE["quitting"]:
            browser_args = args.copy()
            if pause_picker:
                browser_args[1] += "#schedule-pause"
                pause_picker = False
            browser = subprocess.Popen(browser_args)
            code = browser.wait()
            if code or not tray or not keep_in_tray() or not tray.start():
                break
            wake.wait()
            wake.clear()
            tray.close()
        return code
    finally:
        if tray:
            tray.close()
        stop_task()


def main() -> int:
    import win32api
    import win32event
    import winerror

    # Keep the mutex handle alive for the lifetime of this desktop instance.
    instance = win32event.CreateMutex(None, False, f"RewardsDesktop-{run_scheme(DATA)}-{PORT}")
    existing = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    try:
        return run_desktop(existing)
    finally:
        instance.Close()


def run_desktop(existing: bool) -> int:
    run_id = ACTIVATION[2] if ACTIVATION else None
    if activate_existing(run_id, wait=existing):
        _log("已有实例在运行，已激活并退出本次启动")
        return 0
    if existing or (run_id is not None and port_open(PORT)):
        _log("无法激活已有实例")
        return 1

    start_backend(run_id)
    if os.environ.get("REWARDS_USE_CHROMIUM_UI") == "1":
        return fallback_chromium()

    try:
        import webview  # noqa: PLC0415
        webview.settings["ALLOW_DOWNLOADS"] = True
        runtime = bundled_webview_runtime()
        if runtime is not None:
            webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(runtime)
        elif ACTIVATION:
            from portable_launcher import system_runtime_available

            if not system_runtime_available():
                return fallback_chromium()
    except Exception as exc:  # noqa: BLE001
        _log(f"WebView 初始化失败（{exc}），改用内置浏览器窗口")
        return fallback_chromium()

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    except Exception:
        pass
    ensure_shortcut()

    x, y, w, h = load_geometry()
    api_obj = Api()
    win_kwargs = dict(width=w, height=h, min_size=MIN_SIZE, background_color="#f4f6fa",
                      frameless=True, easy_drag=False, js_api=api_obj)
    if x >= 0 and y >= 0:
        win_kwargs.update(x=x, y=y)

    try:
        window = webview.create_window(TITLE, page_url(), **win_kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"WebView2 启动失败（{exc}），改用内置浏览器窗口")
        return fallback_chromium()

    def open_panel() -> None:
        if STATE["quitting"]:
            return
        window.show()
        if hwnd := window.native.Handle.ToInt64():
            foreground(hwnd)
        STATE["hidden"] = False
        if tray:
            tray.close()

    def open_pause_picker() -> None:
        open_panel()
        if not STATE["quitting"]:
            window.evaluate_js("openSchedulePause()")

    def exit_app() -> None:
        STATE["quitting"] = True
        stop_task()
        api_obj.close()

    tray = make_tray(open_panel, open_pause_picker, exit_app)

    def on_closing(allow_tray: bool = True) -> bool:
        if STATE["quitting"]:
            return True
        if not STATE["hidden"]:
            save_geometry(window)
        if allow_tray and tray and keep_in_tray() and tray.start():
            window.hide()
            STATE["hidden"] = True
            return False
        STATE["quitting"] = True
        stop_task()
        return True

    def native_closing(_sender, args) -> None:
        if not on_closing(str(args.CloseReason) == "UserClosing"):
            args.Cancel = True

    def bind_closing() -> None:
        if hasattr(getattr(window, "native", None), "FormClosing"):
            window.native.FormClosing += native_closing
        else:
            window.events.closing += lambda: on_closing()

    def geometry_keeper():
        """每 3 秒比对一次窗口位置尺寸，变化就写盘（不依赖关闭事件）"""
        last = None
        while True:
            time.sleep(3)
            try:
                if STATE["hidden"] or getattr(window, "maximized", False) or getattr(window, "fullscreen", False):
                    continue
                cur = (int(window.x), int(window.y), int(window.width), int(window.height))
                if cur != last:
                    last = cur
                    save_geometry(window)
            except Exception:
                return

    threading.Timer(1.5, lambda: learn_delta(window, (w, h))).start()
    threading.Thread(target=geometry_keeper, daemon=True).start()
    api_obj.attach(window)
    threading.Thread(target=apply_icon, daemon=True).start()

    try:
        if hasattr(window.events, "before_show"):
            window.events.before_show += bind_closing
        else:
            window.events.closing += lambda: on_closing()
        _log("关闭事件已注册")
    except Exception as exc:  # noqa: BLE001
        _log(f"关闭事件注册失败: {exc}")

    try:
        webview.start(lambda: bind_window_actions(open_panel, api_obj.close))
    except Exception as exc:  # noqa: BLE001
        _log(f"桌面窗口启动失败（{exc}），改用内置浏览器窗口")
        if tray:
            tray.close()
        return fallback_chromium()
    finally:
        if tray:
            tray.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
