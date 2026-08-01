"""Claude Code live-session collection and normalization."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ._core import CLAUDE_SOURCE, parse_iso8601

CLAUDE_LIVE_STATES = {
    "busy": "working",
    "working": "working",
    "blocked": "waiting",
    "waiting": "waiting",
    "idle": "idle",
}
CLAUDE_TERMINAL_STATES = {
    "completed",
    "done",
    "failed",
    "stopped",
}


def _iso_started_at(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return (
                datetime.fromtimestamp(value / 1000, timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value and parse_iso8601(value) is not None:
        return value
    return None


def normalize_sessions(rows: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(rows, list):
        return [], ["Claude output is not a JSON array"]

    sessions: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"Claude row {index} is not an object")
            continue

        raw_state = row.get("state")
        raw_status = row.get("status")
        if raw_state is not None and not isinstance(raw_state, str):
            errors.append(f"Claude row {index} has invalid state")
            continue
        if raw_status is not None and not isinstance(raw_status, str):
            errors.append(f"Claude row {index} has invalid status")
            continue

        reported_state = raw_state or raw_status
        state = reported_state.lower() if isinstance(reported_state, str) else ""
        if state in CLAUDE_TERMINAL_STATES:
            continue
        normalized = CLAUDE_LIVE_STATES.get(state)
        if normalized is None:
            errors.append(f"Claude row {index} has unsupported state/status")
            continue

        session_id = row.get("sessionId") or row.get("id")
        cwd = row.get("cwd")
        started_at = _iso_started_at(row.get("startedAt"))
        if not isinstance(session_id, str) or not session_id:
            errors.append(f"Claude live row {index} has no session ID")
            continue
        if not isinstance(cwd, str) or not cwd:
            errors.append(f"Claude live row {index} has no cwd")
            continue
        if started_at is None:
            errors.append(f"Claude live row {index} has no valid startedAt")
            continue
        pid = row.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            pid = None
        raw_name = row.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name else None
        raw_waiting_for = row.get("waitingFor")
        waiting_for = (
            raw_waiting_for
            if isinstance(raw_waiting_for, str) and raw_waiting_for
            else None
        )
        sessions.append(
            {
                "client": "claude",
                "session_id": session_id,
                "state": normalized,
                "cwd": cwd,
                "pid": pid,
                "started_at": started_at,
                "name": name,
                "waiting_for": waiting_for,
            }
        )
    return sessions, errors


def collect_sessions(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    provider = {"ok": True, "source": CLAUDE_SOURCE}
    try:
        result = runner(
            ["claude", "agents", "--json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {**provider, "ok": False, "error": str(error)}, []
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return {**provider, "ok": False, "error": detail}, []
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return {**provider, "ok": False, "error": f"invalid JSON: {error}"}, []

    sessions, errors = normalize_sessions(rows)
    if errors:
        return {**provider, "ok": False, "error": "; ".join(errors)}, sessions
    return provider, sessions
