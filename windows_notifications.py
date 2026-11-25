"""Start menu identity and local Windows notifications for Rewards."""
from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

AUMID = "Rewards.Desktop.App"


def register_shortcut(root: Path, *, refresh: bool = False) -> Path:
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    path = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Rewards.lnk"
    if path.is_file() and not refresh:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    exe = root / "Rewards.exe"
    arguments = ""
    if not exe.is_file():
        exe = Path(sys.executable)
        if exe.with_name("pythonw.exe").is_file():
            exe = exe.with_name("pythonw.exe")
        arguments = f'"{root / "desktop.py"}"'

    pythoncom.CoInitialize()
    try:
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                          pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.SetPath(str(exe))
        link.SetArguments(arguments)
        link.SetWorkingDirectory(str(root))
        link.SetIconLocation(str(root / "assets" / "icon.ico"), 0)
        link.SetDescription("Rewards")
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(AUMID, pythoncom.VT_LPWSTR))
        store.Commit()
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
    finally:
        pythoncom.CoUninitialize()
    return path


def show(root: Path, title: str, message: str) -> None:
    register_shortcut(root)
    env = dict(os.environ, REWARDS_TOAST_ID=AUMID, REWARDS_TOAST_TITLE=title, REWARDS_TOAST_BODY=message)
    script = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:REWARDS_TOAST_ID)
if ($notifier.Setting -ne 'Enabled') { throw "Windows notification setting: $($notifier.Setting)" }
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02')
$text = $xml.GetElementsByTagName('text')
$text.Item(0).InnerText = $env:REWARDS_TOAST_TITLE
$text.Item(1).InnerText = $env:REWARDS_TOAST_BODY
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier.Show($toast)
"""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env=env, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"Windows 通知发送失败（退出码 {result.returncode}）")
