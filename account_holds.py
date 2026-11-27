"""Persistent account holds shared by the task adapter and the panel."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REASONS = {
    "earning_limited": "微软提示积分获取受限",
    "verification_required": "微软要求验证身份",
    "account_restricted": "微软提示账号受限或异常",
}


def path_for(data: Path, email: str) -> Path:
    name = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return data / "account-holds" / f"{name}.json"


def read_file(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("email"), str) and value.get("reason") in REASONS:
            return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        pass
    return {"reason": "unknown"}


def get(data: Path, email: str) -> dict | None:
    return read_file(path_for(data, email))


def resume(data: Path, email: str) -> None:
    path_for(data, email).unlink(missing_ok=True)
