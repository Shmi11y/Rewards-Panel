"""Build a shareable report from known startup events and run results."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

import run_history

LOG_WINDOW = 64 * 1024
STARTUP_HEADER = re.compile(r"\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\] Rewards 启动")
DOWNLOAD_PROGRESS = re.compile(r"PROGRESS (100|[0-9]{1,2})")
STARTUP_STAGES = {
    "STATUS 正在检查桌面运行环境…": "检查桌面运行环境",
    "STATUS 正在解包 WebView2…": "解包 WebView2",
    "STATUS 正在检查 Chromium，首次启动会自动下载…": "检查 Chromium",
    "STATUS 正在打开 Rewards 面板…": "打开 Rewards 面板",
}
DOWNLOAD_COMPONENTS = {
    "WebView2": "WebView2",
    "Chromium": "Chromium",
    "Chromium 无头内核": "Chromium 无头内核",
}
FAILURE_STAGES = ("WebView2 配置失败", "Chromium 配置失败", "启动失败")
RUN_SOURCES = {"manual": "手动", "daily": "定时", "boot": "开机"}
RUN_STATUSES = {"failed": "失败", "partial": "部分失败", "interrupted": "中断"}
ACCOUNT_STATUSES = {
    "success": "成功", "failed": "失败", "needs_login": "需重新登录",
    "skipped": "已跳过", "stopped": "已停止", "interrupted": "中断",
    "not_run": "未运行", "unknown": "结果未知",
}
RUN_REASONS = {
    "缺少 app/node.exe": "缺少 Node.js",
    "缺少 app/dist/index.js": "缺少任务引擎",
    "缺少 task-data.cjs": "缺少任务启动文件",
    "无法启动引擎或读取运行结果": "无法启动引擎或读取运行结果",
}


def _failure_category(detail: str) -> str:
    detail = detail.casefold()
    if "sha-256" in detail:
        return "下载校验失败"
    if "timed out" in detail or "timeout" in detail or "超时" in detail or "winerror 10060" in detail:
        return "超时"
    if "http error" in detail or "status code" in detail:
        return "服务器响应异常"
    if any(word in detail for word in ("econnreset", "enotfound", "urlopen", "connection", "getaddrinfo",
                                       "winerror 10054", "winerror 10051", "winerror 10061")):
        return "网络连接异常"
    if "winerror 5" in detail or "permission denied" in detail or "access is denied" in detail:
        return "访问文件被拒绝"
    if "no space left" in detail or "winerror 112" in detail:
        return "磁盘空间不足"
    if "启动文件不完整" in detail:
        return "缺少启动文件"
    return "其他错误"


def _startup_attempts(raw: bytes) -> list[list[str]]:
    attempts: list[list[str]] = []
    component = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if match := STARTUP_HEADER.fullmatch(line):
            try:
                started = datetime.fromisoformat(match[1])
            except ValueError:
                continue
            attempts.append([f"启动时间 {started:%Y-%m-%d %H:%M:%S}"])
            component = None
            continue
        if not attempts:
            continue
        entries = attempts[-1]
        if line in STARTUP_STAGES:
            entries.append(STARTUP_STAGES[line])
            component = None
        elif line.startswith("DOWNLOAD "):
            name = line.removeprefix("DOWNLOAD ")
            component = DOWNLOAD_COMPONENTS.get(name)
            if re.fullmatch(r"FFmpeg \(playwright ffmpeg v[0-9]{1,9}\)", name):
                component = "FFmpeg"
            elif re.fullmatch(r"Winldd \(playwright winldd v[0-9]{1,9}\)", name):
                component = "Winldd"
            if component:
                entries.append(f"下载 {component}")
        elif component and (match := DOWNLOAD_PROGRESS.fullmatch(line)):
            label = f"{component} 下载进度 "
            progress = f"{label}{int(match[1])}%"
            if entries[-1].startswith(label):
                entries[-1] = progress
            else:
                entries.append(progress)
        else:
            for stage in FAILURE_STAGES:
                if line.startswith(stage + " "):
                    entries.append(f"{stage}（{_failure_category(line[len(stage):])}）")
                    component = None
                    break
    return attempts


def _startup_report(path: Path) -> list[str]:
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(0)
            head = stream.read(LOG_WINDOW)
            first = _startup_attempts(head)
            if size > LOG_WINDOW:
                stream.seek(size - LOG_WINDOW - 1)
                tail = stream.read(LOG_WINDOW + 1)
                _, _, tail = tail.partition(b"\n")
                last = _startup_attempts(tail)
            else:
                last = first
    except FileNotFoundError:
        return ["暂无启动日志"]
    except OSError:
        return ["启动日志暂时无法读取"]
    if not first and not last:
        return ["启动日志中没有可导出的事件"]

    report = []
    if first:
        report.extend(("最早可读启动记录", *first[0][:40]))
        if len(first[0]) > 40 or (size > LOG_WINDOW and len(first) == 1):
            report.append("启动记录已截取")
    if last and (not first or last[-1][0] != first[0][0] or len(first) > 1 or len(last) > 1):
        report.extend(("", "最近可读启动记录", *last[-1][-40:]))
        if len(last[-1]) > 40:
            report.append("启动记录已截取")
    return report


def _run_summary(data: Path) -> list[str]:
    try:
        runs = run_history.read(data)
    except (sqlite3.Error, ValueError):
        return ["暂时无法读取运行记录"]
    for run in runs:
        if run["status"] not in RUN_STATUSES:
            continue
        source = RUN_SOURCES.get(run["source"], "其他")
        result = RUN_STATUSES[run["status"]]
        summary = [f"来源 {source}", f"结果 {result}"]
        try:
            started = datetime.fromtimestamp(run["startedAt"])
        except (OSError, OverflowError, TypeError, ValueError):
            pass
        else:
            summary.insert(0, f"开始时间 {started:%Y-%m-%d %H:%M:%S}")
        if type(run["exitCode"]) is int:
            summary.append(f"退出码 {run['exitCode']}")
        reason = run["reason"]
        if reason in RUN_REASONS:
            summary.append(f"原因 {RUN_REASONS[reason]}")
        elif isinstance(reason, str) and re.fullmatch(r"引擎退出，未返回账号结果（退出码 -?[0-9]+）", reason):
            summary.append("原因 引擎未返回账号结果")
        accounts = run["accounts"]
        results = Counter(
            account["status"] for account in accounts
            if isinstance(account, dict) and isinstance(account.get("status"), str)
        ) if isinstance(accounts, list) else Counter()
        summary.extend(f"{label} {results[status]} 个账号"
                       for status, label in ACCOUNT_STATUSES.items() if results[status])
        return summary
    return ["最近 7 天没有失败或中断的运行记录"]


def make_report(root: Path, data: Path, portable_version: str | None, engine_version: str) -> str:
    engine = engine_version if re.fullmatch(r"v?[0-9]{1,9}(?:\.[0-9]{1,9}){2,3}", engine_version) else "未知"
    lines = [
        "Rewards Panel 脱敏诊断报告",
        f"便携版 {portable_version or '未知'}",
        f"任务引擎 {engine}",
        "", "启动日志节选", *_startup_report(root / "logs" / "launcher.log"),
        "", "最近一次运行失败摘要", *_run_summary(data),
    ]
    return "\n".join(lines) + "\n"
