#!/usr/bin/env -S uv run python
"""Track live Codex turns and report cross-client agent-session status."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

SCHEMA_VERSION = 1
CODEX_SOURCE = "hook-registry"
CLAUDE_SOURCE = "claude-agents-json"
LIVE_CODEX_STATE = "in_flight"
REGISTRY_RELATIVE_PATH = Path(".tmp/agent-session-status")
HOOK_EVENTS = ("UserPromptSubmit", "Stop", "SessionEnd")
RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "turn_id",
        "cwd",
        "state",
        "started_at",
        "updated_at",
        "pid",
        "process_start_fingerprint",
    }
)
CLAUDE_LIVE_STATES = {
    "working": "working",
    "blocked": "waiting",
    "waiting": "waiting",
}
CLAUDE_TERMINAL_STATES = {
    "completed",
    "done",
    "failed",
    "idle",
    "stopped",
}
HOOK_COMMAND_RE = re.compile(r"(?:^|\s)hook(?:\s|$)")

ProcessIdentity = tuple[int, str]
FingerprintLookup = Callable[[int], str | None]


def _warn(message: str, stderr: TextIO = sys.stderr) -> None:
    print(f"agent-session-status: {message}", file=stderr)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex"


def _registry_dir(codex_home: Path) -> Path:
    return codex_home / REGISTRY_RELATIVE_PATH


def _ensure_registry_dir(registry_dir: Path) -> None:
    registry_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry_dir.chmod(0o700)


def _record_path(registry_dir: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return registry_dir / f"{digest}.json"


def _ps_value(pid: int, field: str) -> str:
    try:
        result = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def process_start_fingerprint(pid: int) -> str | None:
    """Return a stable fingerprint for one OS process lifetime."""
    started = _ps_value(pid, "lstart")
    if not started:
        return None
    return hashlib.sha256(started.encode("utf-8")).hexdigest()


def _looks_like_codex_process(command_name: str, command: str) -> bool:
    def is_codex_name(value: str) -> bool:
        name = Path(value).name.lower()
        return (
            name == "codex"
            or name == "codex.exe"
            or name == "codex.js"
            or name.startswith("codex-")
        )

    if is_codex_name(command_name):
        return True

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(is_codex_name(token) for token in tokens[:4])


def codex_process_identity() -> ProcessIdentity | None:
    """Find the Codex ancestor that launched this hook."""
    pid = os.getppid()
    visited: set[int] = set()

    for _ in range(16):
        if pid <= 1 or pid in visited:
            break
        visited.add(pid)

        command_name = _ps_value(pid, "comm")
        command = _ps_value(pid, "command")
        if _looks_like_codex_process(command_name, command):
            fingerprint = process_start_fingerprint(pid)
            if fingerprint:
                return pid, fingerprint

        parent = _ps_value(pid, "ppid")
        try:
            pid = int(parent)
        except ValueError:
            break

    return None


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or set(data) != RECORD_FIELDS:
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    string_fields = (
        "session_id",
        "turn_id",
        "cwd",
        "state",
        "started_at",
        "updated_at",
        "process_start_fingerprint",
    )
    if any(not isinstance(data.get(field), str) for field in string_fields):
        return None
    if data.get("state") != LIVE_CODEX_STATE:
        return None
    if not isinstance(data.get("pid"), int) or data["pid"] <= 0:
        return None
    return data


def _atomic_write_record(path: Path, record: dict[str, Any]) -> None:
    temporary = path.parent / (
        f".{path.stem}.tmp-{os.getpid()}-{os.urandom(6).hex()}"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"), sort_keys=True)
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


def _remove_record(registry_dir: Path, session_id: str) -> None:
    try:
        _record_path(registry_dir, session_id).unlink()
    except FileNotFoundError:
        pass


def prune_registry(
    registry_dir: Path,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    stderr: TextIO = sys.stderr,
) -> None:
    """Remove malformed records and records whose process lifetime ended."""
    if not registry_dir.is_dir():
        return

    for path in registry_dir.glob("*.json"):
        record = _read_record(path)
        stale = record is None
        if record is not None:
            stale = fingerprint_lookup(record["pid"]) != record[
                "process_start_fingerprint"
            ]
        if not stale:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            _warn(f"could not prune {path.name}: {error}", stderr)


def handle_hook_event(
    data: dict[str, Any],
    registry_dir: Path,
    process_identity: ProcessIdentity | None = None,
    now: str | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    stderr: TextIO = sys.stderr,
) -> None:
    """Apply one Codex lifecycle event to the live-turn registry."""
    _ensure_registry_dir(registry_dir)
    prune_registry(registry_dir, fingerprint_lookup=fingerprint_lookup, stderr=stderr)

    event = data.get("hook_event_name")
    session_id = data.get("session_id")
    if event not in HOOK_EVENTS:
        raise ValueError("unsupported or missing hook_event_name")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session_id")

    if event in {"Stop", "SessionEnd"}:
        _remove_record(registry_dir, session_id)
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
    timestamp = now or _utc_now()
    path = _record_path(registry_dir, session_id)
    existing = _read_record(path)
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
    _atomic_write_record(path, record)


def run_hook(
    stdin: TextIO = sys.stdin,
    stderr: TextIO = sys.stderr,
    codex_home: Path | None = None,
) -> int:
    """Consume one hook payload. Failures are advisory and never block Codex."""
    try:
        data = json.load(stdin)
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")
        handle_hook_event(data, _registry_dir(codex_home or _codex_home()), stderr=stderr)
    except Exception as error:  # Hook mode must remain non-blocking.
        _warn(str(error), stderr)
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
            if (
                handler.get("type") == "command"
                and isinstance(command, str)
                and script in command
                and HOOK_COMMAND_RE.search(command)
            ):
                return True
    return False


def _registration_error(codex_home: Path, script_path: Path) -> str | None:
    hooks_path = codex_home / "hooks.json"
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


def collect_codex_sessions(
    codex_home: Path | None = None,
    script_path: Path | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    home = codex_home or _codex_home()
    source = {"ok": True, "source": CODEX_SOURCE}
    registration_error = _registration_error(
        home, script_path or Path(__file__).resolve()
    )
    if registration_error:
        return {**source, "ok": False, "error": registration_error}, []

    registry_dir = _registry_dir(home)
    if not registry_dir.exists():
        return source, []
    if not registry_dir.is_dir():
        return {
            **source,
            "ok": False,
            "error": f"registry is not a directory: {registry_dir}",
        }, []

    sessions: list[dict[str, Any]] = []
    invalid_records = 0
    for path in registry_dir.glob("*.json"):
        record = _read_record(path)
        if record is None:
            invalid_records += 1
            continue
        if fingerprint_lookup(record["pid"]) != record["process_start_fingerprint"]:
            continue
        sessions.append(
            {
                "client": "codex",
                "session_id": record["session_id"],
                "state": LIVE_CODEX_STATE,
                "cwd": record["cwd"],
                "pid": record["pid"],
                "started_at": record["started_at"],
            }
        )

    if invalid_records:
        source = {
            **source,
            "ok": False,
            "error": f"{invalid_records} invalid registry record(s)",
        }
    return source, sessions


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
    if isinstance(value, str) and value:
        return value
    return None


def normalize_claude_sessions(
    rows: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
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
        state = raw_state.lower() if isinstance(raw_state, str) else ""
        status = raw_status.lower() if isinstance(raw_status, str) else ""
        if state in CLAUDE_TERMINAL_STATES:
            continue
        normalized = CLAUDE_LIVE_STATES.get(state)
        if normalized is None:
            normalized = CLAUDE_LIVE_STATES.get(status)
        if normalized is None:
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
        sessions.append(
            {
                "client": "claude",
                "session_id": session_id,
                "state": normalized,
                "cwd": cwd,
                "pid": pid,
                "started_at": started_at,
            }
        )
    return sessions, errors


def collect_claude_sessions(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = {"ok": True, "source": CLAUDE_SOURCE}
    try:
        result = runner(
            ["claude", "agents", "--json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {**source, "ok": False, "error": str(error)}, []
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return {**source, "ok": False, "error": detail}, []
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return {**source, "ok": False, "error": f"invalid JSON: {error}"}, []

    sessions, errors = normalize_claude_sessions(rows)
    if errors:
        return {**source, "ok": False, "error": "; ".join(errors)}, sessions
    return source, sessions


def build_inventory() -> dict[str, Any]:
    codex_provider, codex_sessions = collect_codex_sessions()
    claude_provider, claude_sessions = collect_claude_sessions()
    sessions = [*codex_sessions, *claude_sessions]
    client_order = {"codex": 0, "claude": 1}
    sessions.sort(
        key=lambda row: (
            client_order.get(row["client"], 99),
            row["started_at"],
            row["session_id"],
        )
    )
    providers = {"codex": codex_provider, "claude": claude_provider}
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": all(provider["ok"] for provider in providers.values()),
        "providers": providers,
        "sessions": sessions,
    }


def _human_status(document: dict[str, Any]) -> str:
    lines: list[str] = []
    sessions = document["sessions"]
    if sessions:
        lines.append("CLIENT\tSTATE\tSESSION\tCWD")
        for session in sessions:
            lines.append(
                "\t".join(
                    (
                        session["client"],
                        session["state"],
                        session["session_id"],
                        session["cwd"],
                    )
                )
            )
    else:
        lines.append("No executing agent sessions.")

    provider_parts = []
    failed = []
    for name, provider in document["providers"].items():
        provider_state = "ok" if provider["ok"] else "unavailable"
        provider_parts.append(f"{name}={provider_state} ({provider['source']})")
        if not provider["ok"]:
            failed.append(f"{name}: {provider.get('error', 'unknown error')}")
    lines.append(f"Coverage: {'; '.join(provider_parts)}")
    if failed:
        lines.append(f"WARNING: incomplete provider coverage — {'; '.join(failed)}")
    return "\n".join(lines) + "\n"


def run_status(json_output: bool, stdout: TextIO = sys.stdout) -> int:
    document = build_inventory()
    if json_output:
        json.dump(document, stdout, indent=2)
        stdout.write("\n")
    else:
        stdout.write(_human_status(document))
    return 0 if document["complete"] else 2


def _usage(stderr: TextIO | None = None) -> int:
    print(
        "usage: agent_session_status.py hook | status [--json]",
        file=stderr or sys.stderr,
    )
    return 64


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "hook":
        if len(arguments) != 1:
            _warn("hook mode ignores command-line arguments")
        return run_hook()
    if not arguments or arguments[0] != "status":
        return _usage()
    options = arguments[1:]
    if options not in ([], ["--json"]):
        return _usage()
    return run_status(json_output=bool(options))


if __name__ == "__main__":
    raise SystemExit(main())
