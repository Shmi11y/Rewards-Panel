"""Prepare a local WebView2 runtime for the Windows portable launcher."""
from __future__ import annotations

import argparse
import hashlib
from http.client import HTTPException
import os
from pathlib import Path
import re
import subprocess
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
CABINET_SIZE = 437_849_994
RUNTIME_KEY = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW_SETUP_FAILED = 10
CHROMIUM_SETUP_FAILED = 11
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DOWNLOAD_STARTED = re.compile(r"^Downloading (.+?)\s+from https?://")
DOWNLOAD_PERCENT = re.compile(r"^\|.*\|\s*(\d{1,3})%\s+of\b")


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
    print("DOWNLOAD WebView2", flush=True)
    digest = hashlib.sha256()
    received = 0
    last_percent = 0
    with urlopen(CABINET_URL, timeout=30) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
            received += len(chunk)
            percent = min(99, received * 100 // CABINET_SIZE)
            if percent != last_percent:
                print(f"PROGRESS {percent}", flush=True)
                last_percent = percent
    if received != CABINET_SIZE or digest.hexdigest() != CABINET_SHA256:
        raise RuntimeError("Microsoft WebView2 download failed SHA-256 verification")
    print("PROGRESS 100", flush=True)


def prepare(root: Path) -> None:
    runtime = root / "runtime" / "webview2"
    executable = runtime / "msedgewebview2.exe"
    if executable.is_file() or system_runtime_available():
        return

    runtime.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="webview2-", dir=runtime.parent) as temporary:
        staging = Path(temporary)
        cabinet = staging / f"{CABINET_NAME}.cab"
        download_cabinet(cabinet)
        print("STATUS 正在解包 WebView2…", flush=True)

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


def browser_component(title: str) -> str:
    name = title.lower()
    if name.startswith(("chrome headless shell", "chromium headless shell")):
        return "Chromium 无头内核"
    if name.startswith(("chrome for testing", "chromium")):
        return "Chromium"
    return title


def install_chromium(node: Path, cli: Path, env: dict[str, str]) -> None:
    command = [str(node), str(cli), "install", "chromium"]
    with subprocess.Popen(
        command,
        cwd=node.parent,
        env={**env, "FORCE_COLOR": "0"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        assert process.stdout is not None
        downloading = False
        for output in process.stdout:
            line = ANSI_ESCAPE.sub("", output).strip()
            if not line:
                continue
            print(line, flush=True)
            started = DOWNLOAD_STARTED.match(line)
            if started:
                print(f"DOWNLOAD {browser_component(started[1])}", flush=True)
                downloading = True
            elif downloading:
                progress = DOWNLOAD_PERCENT.match(line)
                if progress:
                    print(f"PROGRESS {int(progress[1])}", flush=True)
                elif " downloaded to " in line:
                    print("PROGRESS 100", flush=True)
                    downloading = False
        returncode = process.wait()
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)


def launch(root: Path, *, interactive: bool = False, use_chromium: bool = False) -> int:
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

    if not use_chromium:
        print("STATUS 正在检查桌面运行环境…", flush=True)
        try:
            prepare(root)
        except (OSError, HTTPException, RuntimeError, subprocess.SubprocessError) as error:
            print(f"WebView2 配置失败 {error}", flush=True)
            if interactive:
                return WEBVIEW_SETUP_FAILED
            use_chromium = True
    if use_chromium:
        env["REWARDS_USE_CHROMIUM_UI"] = "1"
        print("STATUS 正在准备桌面备用 Chromium…", flush=True)
        try:
            install_chromium(node, browser_cli, env)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Chromium 配置失败 {error}", flush=True)
            return CHROMIUM_SETUP_FAILED

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


    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--use-chromium", action="store_true")
    options = parser.parse_args()
    try:
        result = launch(
            Path(__file__).resolve().parent,
            interactive=options.interactive,
            use_chromium=options.use_chromium,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"启动失败 {error}", flush=True)
        result = 1
    raise SystemExit(result)
