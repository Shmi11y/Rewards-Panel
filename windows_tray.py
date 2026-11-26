"""Windows notification area icon and menu, using the bundled pywin32."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import win32api
import win32con
import win32gui
from pywintypes import error as Win32Error


class Tray:
    CALLBACK = win32con.WM_APP + 1
    OPEN, PAUSE, EXIT, TOMORROW, CHOOSE_TIME = 1, 2, 3, 4, 5

    def __init__(self, icon: Path, open_panel, schedule_status, set_paused, open_pause_picker, exit_app, log) -> None:
        self.icon = icon
        self.open_panel = open_panel
        self.schedule_status = schedule_status
        self.set_paused = set_paused
        self.open_pause_picker = open_pause_picker
        self.exit_app = exit_app
        self.log = log
        self.hwnd = 0
        self.icon_handle = 0
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.registered = False

    def start(self) -> bool:
        if self.thread is None or not self.thread.is_alive():
            self.ready.clear()
            self.thread = threading.Thread(target=self._run, name="Rewards tray", daemon=True)
            self.thread.start()
        return self.ready.wait(4) and self.registered

    def close(self) -> None:
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Win32Error:
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def _run(self) -> None:
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = win32api.GetModuleHandle(None)
        window_class.lpszClassName = f"RewardsTrayWindow_{os.getpid()}"
        window_class.lpfnWndProc = {
            self.CALLBACK: self._notification,
            win32con.WM_DESTROY: self._destroy,
            win32gui.RegisterWindowMessage("TaskbarCreated"): self._restore_icon,
        }
        atom = 0
        try:
            atom = win32gui.RegisterClass(window_class)
            self.hwnd = win32gui.CreateWindow(
                atom, "Rewards 托盘", 0, 0, 0, 0, 0, 0, 0, window_class.hInstance, None,
            )
            self.icon_handle = win32gui.LoadImage(
                0, str(self.icon), win32con.IMAGE_ICON, 0, 0, win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
            )
            self._add_icon()
            self.ready.set()
            win32gui.PumpMessages()
        except Exception as exc:
            self.log(f"托盘不可用: {exc}")
        finally:
            if self.hwnd and win32gui.IsWindow(self.hwnd):
                win32gui.DestroyWindow(self.hwnd)
            if self.icon_handle:
                win32gui.DestroyIcon(self.icon_handle)
                self.icon_handle = 0
            if atom:
                win32gui.UnregisterClass(atom, window_class.hInstance)
            self.ready.set()

    def _add_icon(self) -> None:
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (
            self.hwnd, 1, flags, self.CALLBACK, self.icon_handle, "Rewards",
        ))
        self.registered = True

    def _restore_icon(self, *_args) -> int:
        try:
            self._add_icon()
        except Win32Error as exc:
            self.log(f"恢复托盘图标失败: {exc}")
        return 0

    def _destroy(self, _hwnd, _msg, _wparam, _lparam) -> int:
        if self.registered:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 1))
            except Win32Error:
                pass
        self.registered = False
        self.hwnd = 0
        win32gui.PostQuitMessage(0)
        return 0

    def _notification(self, _hwnd, _msg, _wparam, event) -> int:
        if event == win32con.WM_LBUTTONDBLCLK:
            threading.Thread(target=self.open_panel, daemon=True).start()
        elif event in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
            self._menu()
        return 0

    def _menu(self) -> None:
        status = self.schedule_status()
        enabled = status.get("mode") in ("daily", "boot")
        paused = status.get("paused") is True
        if not status.get("mode"):
            next_run = "暂无法读取"
        elif enabled:
            next_run = status.get("nextRun")
        else:
            next_run = "未启用"
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, self.OPEN, "打开面板")
            deadline = status.get("pauseUntil")
            info = f"暂停至：{deadline or '手动恢复'}" if enabled and paused else f"下次运行：{next_run or '—'}"
            win32gui.AppendMenu(menu, win32con.MF_STRING | win32con.MF_GRAYED, 0, info)
            label = "立即恢复" if paused else "暂停定时（手动恢复）"
            flags = win32con.MF_STRING if enabled else win32con.MF_STRING | win32con.MF_GRAYED
            win32gui.AppendMenu(menu, flags, self.TOMORROW, "暂停到明天")
            win32gui.AppendMenu(menu, flags, self.CHOOSE_TIME, "暂停到指定时间…")
            win32gui.AppendMenu(menu, flags, self.PAUSE, label)
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, self.EXIT, "退出程序")
            x, y = win32gui.GetCursorPos()
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Win32Error:
                pass
            flags = win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON
            choice = win32gui.TrackPopupMenu(menu, flags, x, y, 0, self.hwnd, None)
        except Win32Error as exc:
            self.log(f"托盘菜单失败: {exc}")
            return
        finally:
            win32gui.DestroyMenu(menu)
            if self.hwnd:
                win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)

        command = {self.OPEN: self.open_panel, self.PAUSE: lambda: self.set_paused(not paused),
                   self.TOMORROW: lambda: self.set_paused(True, "tomorrow"), self.CHOOSE_TIME: self.open_pause_picker,
                   self.EXIT: self.exit_app}.get(choice)
        if command is not None:
            threading.Thread(target=command, daemon=True).start()
