"""Start menu identity and local Windows notifications for Rewards."""
from __future__ import annotations

import base64
from hashlib import sha256
import os
import re
import subprocess
import sys
from pathlib import Path

AUMID = "Rewards.Desktop.App"
# Protocol activation uses the registered URI handler, so the shortcut only
# needs a placeholder CLSID for Windows to retain notifications.
TOAST_CLSID = "{AA64FBC0-B3BD-461F-A0E9-EC27B0B6E89D}"
SHORTCUT_NAME = "Rewards.lnk"
MAX_RUN_ID = 2**53 - 1


def valid_run_id(value) -> bool:
    return type(value) is int and 0 < value <= MAX_RUN_ID


def run_scheme(data: Path) -> str:
    key = os.path.normcase(str(data.resolve()))
    return "rewards-panel-run-" + sha256(key.encode("utf-8")).hexdigest()[:16]


def parse_activation(args: list[str]) -> tuple[Path, int, int] | None:
    if len(args) != 3:
        return None
    data, port, uri = args
    if not re.fullmatch(r"[1-9][0-9]{0,4}", port) or int(port) > 65535:
        return None
    directory = Path(data)
    if not directory.is_absolute():
        return None
    match = re.fullmatch(run_scheme(directory) + r"://run/([1-9][0-9]{0,15})", uri)
    if not match or not valid_run_id(run_id := int(match[1])):
        return None
    return directory, int(port), run_id


def _register_protocol(root: Path, data: Path) -> str:
    import winreg

    bundled = root / "runtime" / "python" / "pythonw.exe"
    python = Path(sys.executable).with_name("pythonw.exe")
    if bundled.is_file():
        python = bundled
    elif not python.is_file():
        python = Path(sys.executable)
    scheme = run_scheme(data)
    port = int(os.environ.get("REWARDS_PORT", "8765"))
    arguments = [str(python), "-X", "utf8", str(root / "desktop.py"), str(data.resolve()), str(port)]
    command = subprocess.list2cmdline(arguments) + ' "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{scheme}") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Rewards Panel Run")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(key, r"shell\open\command") as handler:
            winreg.SetValueEx(handler, "", 0, winreg.REG_SZ, command)
    return scheme


def register_shortcut(root: Path, *, refresh: bool = False) -> Path:
    import pythoncom
    import pywintypes
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    path = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs" / SHORTCUT_NAME
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
        # pywin32's pscon does not expose this property on all versions.
        activator_key = (pscon.PKEY_AppUserModel_ID[0], 26)
        store.SetValue(activator_key,
                       propsys.PROPVARIANTType(pywintypes.IID(TOAST_CLSID), pythoncom.VT_CLSID))
        store.Commit()
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
    finally:
        pythoncom.CoUninitialize()
    return path


def show(root: Path, data: Path, run_id: int, title: str, message: str) -> None:
    if not valid_run_id(run_id):
        raise ValueError("运行编号不合法")
    scheme = _register_protocol(root, data)
    register_shortcut(root, refresh=True)
    env = dict(os.environ, REWARDS_TOAST_ID=AUMID, REWARDS_TOAST_TITLE=title,
               REWARDS_TOAST_BODY=message, REWARDS_TOAST_URI=f"{scheme}://run/{run_id}")
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
    [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:REWARDS_TOAST_ID)
    $setting = $notifier.Setting
    if ($null -ne $setting -and $setting -ne 'Enabled') { throw "Windows notification setting: $setting" }
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null
    $xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
    $xml.LoadXml('<toast activationType="protocol"><visual><binding template="ToastGeneric"><text/><text/></binding></visual><actions><action content="查看运行记录" activationType="protocol"/></actions></toast>')
    $xml.DocumentElement.SetAttribute('launch', $env:REWARDS_TOAST_URI)
    $xml.GetElementsByTagName('action').Item(0).SetAttribute('arguments', $env:REWARDS_TOAST_URI)
    $text = $xml.GetElementsByTagName('text')
    $text.Item(0).InnerText = $env:REWARDS_TOAST_TITLE
    $text.Item(1).InnerText = $env:REWARDS_TOAST_BODY
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $notifier.Show($toast)
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env=env, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Windows 通知发送失败（退出码 {result.returncode}）{error}")
