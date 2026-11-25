"""Prepare a local WebView2 runtime for the Windows portable launcher."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import urlopen
import winreg


WEBVIEW2_VERSION = "158.0.4370.33"
CABINET_NAME = f"Microsoft.WebView2.FixedVersionRuntime.{WEBVIEW2_VERSION}.x64"
CABINET_URL = (
    "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/"
    f"ecc2761e-1380-46ee-9552-995b039e4e47/{CABINET_NAME}.cab"
)
CABINET_SHA256 = "835da387df67797f710881f553bb6934d98f24d08e370456f48d38ed4e072b62"
RUNTIME_KEY = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def system_runtime_available() -> bool:
    for hive, flags in (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
    ):
        try:
            with winreg.OpenKey(hive, RUNTIME_KEY, 0, flags) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
            if version != "0.0.0.0":
                return True
    return False


def download_cabinet(destination: Path) -> None:
    digest = hashlib.sha256()
    with urlopen(CABINET_URL, timeout=30) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != CABINET_SHA256:
        raise RuntimeError("Microsoft WebView2 download failed SHA-256 verification")


def prepare(root: Path) -> None:
    runtime = root / "runtime" / "webview2"
    executable = runtime / "msedgewebview2.exe"
    if executable.is_file() or system_runtime_available():
        return

    runtime.parent.mkdir(parents=True, exist_ok=True)
    print("STATUS 正在下载 WebView2，约 418 MiB…", flush=True)
    with TemporaryDirectory(prefix="webview2-", dir=runtime.parent) as temporary:
        staging = Path(temporary)
        cabinet = staging / f"{CABINET_NAME}.cab"
        download_cabinet(cabinet)

        extracted = staging / "extracted"
        extracted.mkdir()
        expand = Path(os.environ["SystemRoot"]) / "System32" / "expand.exe"
        subprocess.run(
            [str(expand), str(cabinet), "-F:*", str(extracted)],
            stdout=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
        )
        prepared = extracted / CABINET_NAME
        if not (prepared / executable.name).is_file():
            raise RuntimeError("The Microsoft archive did not contain WebView2")
        try:
            prepared.rename(runtime)
        except OSError:
            if not executable.is_file():
                raise


def launch(root: Path) -> None:
    pythonw = root / "runtime" / "python" / "pythonw.exe"
    desktop = root / "desktop.py"
    node = root / "app" / "node.exe"
    browser_cli = root / "app" / "node_modules" / "patchright" / "cli.js"
    for entry in (pythonw, desktop, node, browser_cli):
        if not entry.is_file():
            raise FileNotFoundError(f"启动文件不完整 {entry.relative_to(root)}")

    env = os.environ.copy()
    env.update({
        "REWARDS_APP_ROOT": str(root),
        "REWARDS_DATA_DIR": str(root / "data"),
        "PLAYWRIGHT_BROWSERS_PATH": str(root / "browser"),
    })
    env.pop("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", None)
    env.pop("REWARDS_USE_CHROMIUM_UI", None)

    print("STATUS 正在检查桌面运行环境…", flush=True)
    try:
        prepare(root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"WebView2 配置失败，将使用 Chromium 窗口。{error}", flush=True)
        env["REWARDS_USE_CHROMIUM_UI"] = "1"

    print("STATUS 正在检查 Chromium，首次启动会自动下载…", flush=True)
    subprocess.run(
        [str(node), str(browser_cli), "install", "chromium"],
        cwd=root / "app",
        env=env,
        stdout=sys.stdout,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=True,
    )

    print("STATUS 正在打开 Rewards 面板…", flush=True)
    log_directory = root / "logs"
    log_directory.mkdir(exist_ok=True)
    with (log_directory / "desktop-startup.log").open("ab") as log:
        subprocess.Popen(
            [str(pythonw), "-X", "utf8", str(desktop)],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )


if __name__ == "__main__":
    try:
        launch(Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"启动失败 {error}", flush=True)
        raise SystemExit(1)
