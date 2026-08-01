"""Codex hook lifecycle and live-session collection."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any, TextIO

from ._core import (
    CODEX_SOURCE,
    HOOK_EVENTS,
    IDLE_CODEX_STATE,
    LIVE_CODEX_STATE,
    SCHEMA_VERSION,
    atomic_write_json,
    registry_dir,
    utc_now,
    warn,
)
from ._core import (
    codex_home as default_codex_home,
)
from .process import (
    FingerprintLookup,
    ProcessIdentity,
    codex_process_identity,
    process_start_fingerprint,
)
from .registry import (
    ensure_registry_dir,
    prune_registry,
    read_record,
    record_path,
    remove_record,
)

ENTRYPOINT_PATH = Path(__file__).resolve().parent.parent / "agent_session_status.py"


def handle_hook_event(
    data: dict[str, Any],
    registry: Path,
    process_identity: ProcessIdentity | None = None,
    now: str | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    stderr: TextIO = sys.stderr,
) -> None:
    """Apply one Codex lifecycle event to the live-turn registry."""
    ensure_registry_dir(registry)

    event = data.get("hook_event_name")
    session_id = data.get("session_id")
    if event not in HOOK_EVENTS:
        raise ValueError("unsupported or missing hook_event_name")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session_id")

    timestamp = now or utc_now()
    if event == "SessionEnd":
        remove_record(registry, session_id)
        prune_registry(
            registry,
            fingerprint_lookup=fingerprint_lookup,
            stderr=stderr,
            now=timestamp,
        )
        return

    if event == "Stop":
        path = record_path(registry, session_id)
        existing = read_record(path, now=timestamp)
        existing_matches = (
            existing is not None
            and fingerprint_lookup(existing["pid"])
            == existing["process_start_fingerprint"]
        )
        identity = (
            (existing["pid"], existing["process_start_fingerprint"])
            if existing_matches
            else process_identity or codex_process_identity()
        )
        raw_turn_id = data.get("turn_id")
        raw_cwd = data.get("cwd")
        turn_id = (
            raw_turn_id
            if isinstance(raw_turn_id, str) and raw_turn_id
            else existing["turn_id"] if existing_matches else None
        )
        cwd = (
            raw_cwd
            if isinstance(raw_cwd, str) and raw_cwd
            else existing["cwd"] if existing_matches else None
        )
        if identity is None or turn_id is None or cwd is None:
            remove_record(registry, session_id)
            prune_registry(
                registry,
                fingerprint_lookup=fingerprint_lookup,
                stderr=stderr,
                now=timestamp,
            )
            return
        pid, fingerprint = identity
        atomic_write_json(
            path,
            {
                "schema_version": SCHEMA_VERSION,
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": cwd,
                "state": IDLE_CODEX_STATE,
                "started_at": existing["started_at"]
                if existing_matches
                else timestamp,
                "updated_at": timestamp,
                "pid": pid,
                "process_start_fingerprint": fingerprint,
            },
        )
        prune_registry(
            registry,
            fingerprint_lookup=fingerprint_lookup,
            stderr=stderr,
            exclude_path=path,
            now=timestamp,
        )
        return

    turn_id = data.get("turn_id")
    cwd = data.get("cwd")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("missing turn_id")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("missing cwd")

    identity = process_identity or codex_process_identity()
    if identity is None:
        raise RuntimeError("could not identify the owning Codex process")
    pid, fingerprint = identity
    path = record_path(registry, session_id)
    existing = read_record(path, now=timestamp)
    started_at = timestamp
    if (
        existing is not None
        and existing["turn_id"] == turn_id
        and existing["pid"] == pid
        and existing["process_start_fingerprint"] == fingerprint
    ):
        started_at = existing["started_at"]

    record = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": cwd,
        "state": LIVE_CODEX_STATE,
        "started_at": started_at,
        "updated_at": timestamp,
        "pid": pid,
        "process_start_fingerprint": fingerprint,
    }
    atomic_write_json(path, record)
    prune_registry(
        registry,
        fingerprint_lookup=fingerprint_lookup,
        stderr=stderr,
        exclude_path=path,
        now=timestamp,
    )


def run_hook(
    stdin: TextIO = sys.stdin,
    stderr: TextIO = sys.stderr,
    codex_home: Path | None = None,
) -> int:
    """Consume one hook payload. Failures are advisory and never block Codex."""
    try:
        data = json.load(stdin)
        if not isinstance(data, dict):
            raise TypeError("hook input must be a JSON object")
        handle_hook_event(
            data, registry_dir(codex_home or default_codex_home()), stderr=stderr
        )
    except Exception as error:  # noqa: BLE001 - Hook mode must never block Codex.
        warn(str(error), stderr)
    return 0


def _hook_registered_for_event(
    hooks: dict[str, Any], event: str, script_path: Path
) -> bool:
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return False
    script = str(script_path)
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        for handler in group["hooks"]:
            if not isinstance(handler, dict):
                continue
            command = handler.get("command")
            if handler.get("type") != "command" or not isinstance(command, str):
                continue
            try:
                command_tokens = shlex.split(command)
            except ValueError:
                continue
            if command_tokens == [script, "hook"]:
                return True
    return False


def _registration_error(home: Path, script_path: Path) -> str | None:
    hooks_path = home / "hooks.json"
    try:
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"missing {hooks_path}"
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return f"could not read {hooks_path}: {error}"
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        return f"invalid hooks configuration in {hooks_path}"
    missing = [
        event
        for event in HOOK_EVENTS
        if not _hook_registered_for_event(hooks, event, script_path)
    ]
    if missing:
        return f"hook is not registered for {', '.join(missing)}"
    return None


def collect_sessions(
    codex_home: Path | None = None,
    script_path: Path | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    now: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved_home = codex_home or default_codex_home()
    provider = {"ok": True, "source": CODEX_SOURCE, "dropped": 0}
    registration_error = _registration_error(
        resolved_home, script_path or ENTRYPOINT_PATH
    )
    if registration_error:
        return {**provider, "ok": False, "error": registration_error}, []

    registry = registry_dir(resolved_home)
    if not registry.exists():
        return provider, []
    if not registry.is_dir():
        return {
            **provider,
            "ok": False,
            "error": f"registry is not a directory: {registry}",
        }, []

    sessions: list[dict[str, Any]] = []
    invalid_records = 0
    try:
        paths = list(registry.glob("*.json"))
    except OSError as error:
        return {**provider, "ok": False, "error": str(error)}, []
    for path in paths:
        record = read_record(path, now=now)
        if record is None:
            invalid_records += 1
            continue
        if fingerprint_lookup(record["pid"]) != record["process_start_fingerprint"]:
            continue
        sessions.append(
            {
                "client": "codex",
                "session_id": record["session_id"],
                "state": record["state"],
                "cwd": record["cwd"],
                "pid": record["pid"],
                "started_at": record["started_at"],
                "updated_at": record["updated_at"],
                "name": None,
                "waiting_for": None,
            }
        )

    return {**provider, "dropped": invalid_records}, sessions
