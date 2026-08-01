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


def normalize_sessions(rows: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(rows, list):
        return [], 0

    sessions: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            dropped += 1
            continue

        raw_state = row.get("state")
        raw_status = row.get("status")
        if raw_state is not None and not isinstance(raw_state, str):
            dropped += 1
            continue
        if raw_status is not None and not isinstance(raw_status, str):
            dropped += 1
            continue

        reported_state = raw_state or raw_status
        if not isinstance(reported_state, str):
            dropped += 1
            continue
        state = reported_state.lower()
        if state in CLAUDE_TERMINAL_STATES:
            continue
        normalized = CLAUDE_LIVE_STATES.get(state, "unknown")

        session_id = row.get("sessionId") or row.get("id")
        cwd = row.get("cwd")
        started_at = _iso_started_at(row.get("startedAt"))
        if not isinstance(session_id, str) or not session_id:
            dropped += 1
            continue
        if not isinstance(cwd, str) or not cwd:
            dropped += 1
            continue
        if started_at is None:
            dropped += 1
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
    return sessions, dropped


def collect_sessions(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    provider = {"ok": True, "source": CLAUDE_SOURCE, "dropped": 0}
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
    if not isinstance(rows, list):
        return {**provider, "ok": False, "error": "JSON output is not an array"}, []

    sessions, dropped = normalize_sessions(rows)
    return {**provider, "dropped": dropped}, sessions
