"""Check official releases and save a verified portable archive."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

RELEASE_API = "https://api.github.com/repos/Shmi11y/Rewards-Panel/releases/latest"
RELEASE_PAGE = "https://github.com/Shmi11y/Rewards-Panel/releases"
RELEASE_VERSION = re.compile(r"v?([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})")
ARCHIVE = "RewardsPanel-windows-x64-lite.zip"
MAX_BYTES = 512 * 1024 * 1024
MAX_METADATA = 1024 * 1024
REQUEST_HEADERS = {"User-Agent": "RewardsPanel"}
USER_FOLDERS = {"data", "browser", "logs", "ui-profile", "updates", ".git"}


def _program_path(root: Path, member: str) -> Path | None:
    if not member.startswith("RewardsPanel/") or member.endswith("/"):
        return None
    parts = member.split("/")[1:]
    if (any(not part or part in {".", ".."} or part.endswith((".", " "))
            or any(char in part for char in ':\\<>|?*"') for part in parts)
            or parts[0].casefold() in USER_FOLDERS or [p.casefold() for p in parts[:2]] == ["app", "logs"]
            or any(part.casefold().startswith(".env")
                   or part.casefold() in {"sessions", "accounts.json", "config.json", "sessions.db"}
                   for part in parts)):
        return None
    path = root.joinpath(*parts)
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents) if p != root):
        return None
    return path


def prepare_rollback(root: Path, archive: Path, tag: str) -> Path:
    """Snapshot installed program files that a verified archive can overwrite."""
    root = root.resolve()
    folder = root / "updates"
    folder.mkdir(exist_ok=True)
    target = folder / "rollback.zip"
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        candidate = Path(temp) / target.name
        with ZipFile(archive) as incoming, ZipFile(candidate, "w", ZIP_DEFLATED, compresslevel=3) as backup:
            names = set()
            for member in incoming.namelist():
                if not member.endswith("/") and _program_path(root, member) is None:
                    raise ValueError("更新包包含需手动检查的路径，无法创建恢复点")
                path = _program_path(root, member)
                if path is not None and path.is_file():
                    if member.casefold() in names:
                        raise ValueError("升级包包含重复文件")
                    backup.write(path, member)
                    names.add(member.casefold())
            if not {"rewardspanel/version", "rewardspanel/rewards.exe", "rewardspanel/panel/server.py"} <= names:
                raise ValueError("没有找到可备份的便携版程序文件")
            backup.writestr("rewards-rollback.json", json.dumps({
                "from": (root / "VERSION").read_text(encoding="utf-8").strip(), "to": tag,
            }))
        candidate.replace(target)
    return target


def rollback_status(root: Path) -> dict:
    folder = root / "updates"
    try:
        with ZipFile(folder / "rollback.zip") as backup:
            result = json.loads(backup.read("rewards-rollback.json"))
    except (OSError, ValueError, KeyError, BadZipFile):
        result = {}
    log = folder / "upgrade-history.txt"
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 4096))
            result["history"] = stream.read().decode("utf-8", errors="replace").splitlines()[-8:]
    except OSError:
        pass
    return result


def record_upgrade(root: Path, version: str | None) -> None:
    status = rollback_status(root)
    if not version or version != str(status.get("to", "")).removeprefix("v"):
        return
    record = f"Detected v{version} (recovery point: v{status.get('from')})"
    if any(line.endswith(record) for line in status.get("history", [])):
        return
    try:
        from datetime import datetime
        with (root / "updates" / "upgrade-history.txt").open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now():%Y-%m-%dT%H:%M:%S} {record}\n")
    except OSError:
        pass


@dataclass(frozen=True)
class Asset:
    tag: str
    sha256: str
    size: int | None

    @property
    def url(self) -> str:
        return f"{RELEASE_PAGE}/download/{self.tag}/{ARCHIVE}"


def _digest(value) -> str | None:
    match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", value) if isinstance(value, str) else None
    return match[1].lower() if match else None


def _get(url: str, *, method: str = "GET", accept: str = "*/*"):
    return urlopen(Request(url, method=method, headers={**REQUEST_HEADERS, "Accept": accept}), timeout=12)


def _metadata(url: str, accept: str) -> str:
    with _get(url, accept=accept) as response:
        raw = response.read(MAX_METADATA + 1)
    if len(raw) > MAX_METADATA:
        raise ValueError("Metadata exceeds the size limit")
    return raw.decode("utf-8")


class _ReleaseNotes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif dict(attrs).get("data-test-selector") == "body-content":
                self.depth = 1
        if self.depth and tag in {"li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if self.depth and tag == "div":
            self.depth -= 1
        if self.depth and tag in {"p", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data) -> None:
        if self.depth:
            self.parts.append(data)


def _public_tag() -> str:
    with _get(f"{RELEASE_PAGE}/latest", method="HEAD") as response:
        url = response.geturl()
    prefix = f"{RELEASE_PAGE}/tag/"
    return url[len(prefix):] if url.lower().startswith(prefix.lower()) else ""


def _web_details(tag: str) -> tuple[str, str | None]:
    notes = _ReleaseNotes()
    try:
        notes.feed(_metadata(f"{RELEASE_PAGE}/tag/{tag}", "text/html"))
    except (OSError, HTTPException, ValueError):
        pass
    digest = None
    try:
        page = _metadata(f"{RELEASE_PAGE}/expanded_assets/{tag}", "text/html")
        asset_path = urlsplit(f"{RELEASE_PAGE}/download/{tag}/{ARCHIVE}").path
        link = f'href="{asset_path}"'.lower()
        row = next((item for item in page.split("</li>") if link in item.lower()), "")
        match = re.search(r"\bsha256:[0-9a-fA-F]{64}\b", row)
        digest = _digest(match[0]) if match else None
    except (OSError, HTTPException, ValueError):
        pass
    return "".join(notes.parts), digest


def _release_summary(body: str) -> str:
    lines = []
    for raw in body.splitlines():
        line = raw.strip().lstrip("#").strip()
        if re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", line) or line.startswith("```"):
            continue
        if "SHA-256" in line.upper() and len(line) < 50:
            continue
        if line:
            lines.append(line)
    return "\n".join(lines)[:4500]


def check(current: str | None) -> tuple[dict, Asset | None]:
    if current is None:
        return {"ok": False, "msg": "无法识别当前便携版版本"}, None
    release = {}
    try:
        try:
            release = json.loads(_metadata(RELEASE_API, "application/vnd.github+json"))
            tag = release.get("tag_name") if isinstance(release, dict) else None
        except HTTPError as error:
            if error.code not in (403, 429):
                raise
            tag = _public_tag()
    except HTTPError as error:
        message = "GitHub 上未找到正式版本" if error.code == 404 else "GitHub 暂时无法提供版本信息，请稍后重试"
        return {"ok": False, "msg": message}, None
    except (OSError, HTTPException):
        return {"ok": False, "msg": "暂时无法连接 GitHub，请检查网络后重试"}, None
    except (ValueError, RecursionError):
        return {"ok": False, "msg": "无法读取 GitHub 版本信息"}, None

    latest = RELEASE_VERSION.fullmatch(tag) if isinstance(tag, str) else None
    if latest is None:
        return {"ok": False, "msg": "无法识别 GitHub 上的版本号"}, None
    available = tuple(map(int, latest.groups())) > tuple(map(int, current.split(".")))
    result = {"ok": True, "current": current, "latest": ".".join(latest.groups()), "updateAvailable": available}
    if not available:
        return result, None

    assets = release.get("assets")
    if not isinstance(assets, list):
        assets = []
    api_asset = next((asset for asset in assets if isinstance(asset, dict) and asset.get("name") == ARCHIVE), {})
    size = api_asset.get("size")
    size = size if type(size) is int and size > 0 else None
    digest = _digest(api_asset.get("digest"))
    notes = release.get("body")
    notes = notes if isinstance(notes, str) else ""
    if not digest:
        web_notes, digest = _web_details(tag)
        notes = notes or web_notes
    asset = Asset(tag, digest, size) if digest and (size is None or size <= MAX_BYTES) else None
    return {**result, "notes": _release_summary(notes), "canDownload": asset is not None,
            "releaseUrl": f"{RELEASE_PAGE}/tag/{tag}", "tag": tag}, asset


class UpdateDownloads:
    def __init__(self, folder: Path, root: Path | None = None) -> None:
        self.folder = folder
        self.root = root
        self._lock = threading.Lock()
        self._asset: Asset | None = None
        self._saved: Path | None = None
        self._verified: Asset | None = None
        self._state = {"state": "idle"}

    def check(self, current: str | None) -> dict:
        result, asset = check(current)
        with self._lock:
            self._asset = asset
        return result

    def status(self) -> dict:
        with self._lock:
            result = dict(self._state)
            try:
                result["installResult"] = json.loads((self.folder / "install-result.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            return result

    def start(self, tag) -> dict:
        with self._lock:
            asset = self._asset
            if self._state["state"] in {"downloading", "verifying", "installing"}:
                return {"ok": False, "msg": "已有更新包正在下载"}
            if asset is None or tag != asset.tag:
                return {"ok": False, "msg": "请重新检查更新，获取可校验的下载包"}
            self._saved = None
            self._verified = None
            self._state = {"state": "downloading", "tag": tag, "received": 0, "total": asset.size}
            try:
                threading.Thread(target=self._download, args=(asset,), daemon=True).start()
            except RuntimeError:
                self._state = {"state": "idle"}
                return {"ok": False, "msg": "无法启动下载，请重试"}
        return {"ok": True}

    def _download(self, asset: Asset) -> None:
        temporary = None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self.folder / f"RewardsPanel-{asset.tag}-windows-x64-lite.zip"
            checksum = hashlib.sha256()
            received = 0
            with (_get(asset.url) as response,
                  tempfile.NamedTemporaryFile(dir=self.folder, suffix=".part", delete=False) as output):
                temporary = Path(output.name)
                final = urlsplit(response.geturl())
                if final.scheme != "https" or final.hostname not in {"github.com", "release-assets.githubusercontent.com",
                                                                     "objects.githubusercontent.com"}:
                    raise ValueError("下载地址未通过校验")
                length = response.headers.get("Content-Length")
                total = asset.size or (int(length) if length and length.isascii() and length.isdecimal() else None)
                if total is not None and not 0 < total <= MAX_BYTES:
                    raise ValueError("更新包大小超出限制")
                while chunk := response.read(256 * 1024):
                    received += len(chunk)
                    if received > (total or MAX_BYTES):
                        raise ValueError("更新包大小与发布信息不符")
                    checksum.update(chunk)
                    output.write(chunk)
                    with self._lock:
                        self._state.update(received=received, total=total)

            with self._lock:
                self._state["state"] = "verifying"
            if total is not None and received != total:
                raise ValueError("下载不完整，请重试")
            if received == 0 or checksum.hexdigest() != asset.sha256:
                raise ValueError("SHA-256 校验失败，请重试")
            temporary.replace(target)
            recovery = None
            recovery_error = None
            if self.root is not None and (self.root / "Rewards.exe").is_file():
                try:
                    recovery = str(prepare_rollback(self.root, target, asset.tag))
                except (OSError, ValueError, BadZipFile) as error:
                    recovery_error = str(error)
            with self._lock:
                self._saved = target
                self._verified = asset
                self._state.update(state="ready", path=str(target), rollback=recovery, rollbackError=recovery_error)
        except (OSError, HTTPException, ValueError) as error:
            message = str(error) if isinstance(error, ValueError) else "无法下载或保存更新包，请检查网络与磁盘空间"
            with self._lock:
                self._state.update(state="failed", msg=message)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def installing(self) -> bool:
        with self._lock:
            return self._state["state"] == "installing"

    def install(self) -> dict:
        with self._lock:
            if self._state["state"] != "ready" or self._saved is None or self._verified is None:
                return {"ok": False, "msg": "请先下载并校验更新包"}
            if not self._state.get("rollback"):
                return {"ok": False, "msg": "未创建恢复点，请重新下载更新包"}
            root = self.root
            if (os.name != "nt" or root is None or not (root / "Rewards.exe").is_file()
                    or not (root / "runtime/python/python.exe").is_file()):
                return {"ok": False, "msg": "一键更新仅适用于 Windows 便携版"}
            work = None
            try:
                if (self.folder.lstat().st_file_attributes & 0x400) != 0:
                    raise OSError("更新目录是链接或联接")
                work = Path(tempfile.mkdtemp(prefix="install-", dir=self.folder))
                runtime = root / "runtime/python"
                for file in runtime.iterdir():
                    if file.is_file():
                        shutil.copy2(file, work / file.name)
                for name in ("updates.py", "update_install.py"):
                    shutil.copy2(Path(__file__).with_name(name), work / name)
                (self.folder / "install-result.json").unlink(missing_ok=True)
                subprocess.Popen(
                    [str(work / "python.exe"), str(work / "update_install.py"), str(root),
                     str(self._saved), self._verified.tag, self._verified.sha256, str(os.getpid())],
                    cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            except OSError as error:
                if work is not None:
                    shutil.rmtree(work, ignore_errors=True)
                return {"ok": False, "msg": f"无法启动自动更新：{error}"}
            self._state["state"] = "installing"
            return {"ok": True}

    def open_folder(self) -> dict:
        with self._lock:
            saved = self._saved
        if saved is None or not saved.is_file():
            return {"ok": False, "msg": "找不到已校验的更新包，请重新下载"}
        try:
            os.startfile(saved.parent)
        except (AttributeError, OSError):
            return {"ok": False, "msg": "无法打开下载目录，请按显示的路径查找更新包"}
        return {"ok": True}
