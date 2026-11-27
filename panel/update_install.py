"""Apply a verified portable update from a private copy of the runtime."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from updates import MAX_BYTES, RELEASE_VERSION, _program_path

NO_WINDOW = subprocess.CREATE_NO_WINDOW
MAX_EXPANDED = 2 * 1024**3


def processes(root):
    script = ("Get-CimInstance Win32_Process -ErrorAction Stop | "
              "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    result = subprocess.check_output([str(shell), "-NoProfile", "-NonInteractive", "-Command", script],
                                     creationflags=NO_WINDOW, timeout=15)
    rows = json.loads(result.decode("utf-8-sig"))
    if isinstance(rows, dict):
        rows = [rows]
    prefix = str(root).casefold() + "\\"
    return [row for row in rows if row["ProcessId"] != os.getpid()
            and (row["ExecutablePath"] or "").casefold().startswith(prefix)]


def scheduler_role(row, root):
    command = (row["CommandLine"] or "").casefold()
    script = re.escape(str(root / "scheduler.py").casefold())
    match = re.search(r'(?:^|\s)(?:"' + script + r'"|' + script + r')(?:\s|$)', command)
    if match:
        return 2 if "--worker" in command[match.end():].split() else 1
    return 0


def wait_for_panel(root, desktop_pid):
    # Wait for the panel to release its runtime before stopping the supervisor.
    deadline = time.monotonic() + 35
    while True:
        rows = processes(root)
        if all(row["ProcessId"] != desktop_pid for row in rows):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("面板未能退出，更新已取消")
        time.sleep(0.4)



def stop_and_wait(root, desktop_pid):
    wait_for_panel(root, desktop_pid)
    for role in (1, 2):
        for row in processes(root):
            if scheduler_role(row, root) == role:
                try:
                    os.kill(row["ProcessId"], 15)
                except ProcessLookupError:
                    pass
    for _ in range(6):
        if not processes(root):
            return
        time.sleep(0.5)
    raise RuntimeError("仍有本目录的进程在运行，更新已取消")


def stage(root, work, archive, tag, digest):
    if not 0 < archive.stat().st_size <= MAX_BYTES:
        raise ValueError("更新包大小超出限制")
    wanted = RELEASE_VERSION.fullmatch(tag)
    current = RELEASE_VERSION.fullmatch((root / "VERSION").read_text(encoding="utf-8").strip())
    if not wanted or not current or tuple(map(int, wanted.groups())) <= tuple(map(int, current.groups())):
        raise ValueError("更新包版本必须高于当前版本")
    incoming = work / "incoming"
    incoming.mkdir()
    package = work / "verified.zip"
    shutil.copyfile(archive, package)
    with package.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("更新包 SHA-256 校验失败")
    changes = []
    seen = set()
    total = 0
    with ZipFile(package) as zip_file:
        for entry in zip_file.infolist():
            if entry.is_dir():
                continue
            member = entry.filename
            target = _program_path(root, member)
            if target is None or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("更新包包含不允许的文件路径")
            relative = target.relative_to(root)
            for path in (target, *target.parents):
                if path == root:
                    break
                if path.exists() and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("程序目录包含链接或联接")
            key = str(relative).casefold()
            if key in seen or any(part.split(".")[0].upper() in
                                  {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                                   *(f"LPT{i}" for i in range(10))} for part in relative.parts):
                raise ValueError("更新包包含重复或无效路径")
            seen.add(key)
            total += entry.file_size
            if total > MAX_EXPANDED:
                raise ValueError("更新包解压大小超出限制")
            source = incoming / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(entry) as reader, source.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            changes.append((relative, source))
    if not {"version", "rewards.exe", "panel\\server.py", "desktop.py"} <= seen:
        raise ValueError("更新包缺少便携版启动文件")
    if (incoming / "VERSION").read_text(encoding="utf-8").strip() != tag.removeprefix("v"):
        raise ValueError("更新包内部版本与发布信息不符")
    return changes


class RollbackError(RuntimeError):
    pass


def replace_files(root, work, changes):
    originals = work / "originals"
    moved = []
    try:
        for relative, source in changes:
            target = root / relative
            backup = None
            if target.is_file():
                backup = originals / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                target.replace(backup)
            moved.append((target, backup, False))
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            moved[-1] = (target, backup, True)
    except OSError:
        rollback_errors = []
        for target, backup, installed in reversed(moved):
            try:
                if installed:
                    target.unlink()
                if backup is not None:
                    backup.replace(target)
            except OSError as error:
                rollback_errors.append(str(error))
        if rollback_errors:
            raise RollbackError("无法完整回滚，请查看更新目录并使用恢复点") from None
        raise


def main():
    root, archive, tag, digest, pid = sys.argv[1:]
    root = Path(root).resolve()
    work = Path(__file__).resolve().parent
    log = work.parent / "install-result.json"
    applied = False
    restart = True
    try:
        changes = stage(root, work, Path(archive), tag, digest)
        stop_and_wait(root, int(pid))
        replace_files(root, work, changes)
        applied = True
        result = {"ok": True, "msg": f"已安装 {tag}"}
    except Exception as error:
        restart = not isinstance(error, RollbackError)
        result = {"ok": False, "msg": f"自动更新未完成：{error}", "work": str(work)}
    try:
        pending = work / "result.json"
        pending.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        pending.replace(log)
    finally:
        # A fresh panel starts the saved daily scheduler using its normal startup path.
        if restart:
            wait_for_panel(root, int(pid))
            subprocess.Popen([str(root / "Rewards.exe")], cwd=root)
    if applied:
        for name in ("incoming", "originals", "verified.zip"):
            path = work / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
