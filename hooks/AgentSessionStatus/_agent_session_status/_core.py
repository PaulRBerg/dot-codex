"""Shared constants and infrastructure for agent-session status."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SCHEMA_VERSION = 1
CODEX_SOURCE = "hook-registry"
CLAUDE_SOURCE = "claude-agents-json"
LIVE_CODEX_STATE = "in_flight"
REGISTRY_RELATIVE_PATH = Path(".tmp/agent-session-status")
HOOK_EVENTS = ("UserPromptSubmit", "Stop", "SessionEnd")


def warn(message: str, stderr: TextIO = sys.stderr) -> None:
    print(f"agent-session-status: {message}", file=stderr)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex"


def registry_dir(home: Path) -> Path:
    return home / REGISTRY_RELATIVE_PATH


def ensure_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def digest_filename(key: str) -> str:
    return f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"


def atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.parent / (f".{path.stem}.tmp-{os.getpid()}-{os.urandom(6).hex()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def parse_iso8601(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def age_seconds(reference: str, now: str) -> int | None:
    reference_dt = parse_iso8601(reference)
    now_dt = parse_iso8601(now)
    if reference_dt is None or now_dt is None:
        return None
    return max(0, int((now_dt - reference_dt).total_seconds()))


def format_age(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"
