#!/usr/bin/env -S uv run python
"""Track live Codex turns and report cross-client agent-session status."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SCHEMA_VERSION = 1
CODEX_SOURCE = "hook-registry"
CLAUDE_SOURCE = "claude-agents-json"
LIVE_CODEX_STATE = "in_flight"
REGISTRY_RELATIVE_PATH = Path(".tmp/agent-session-status")
HOOK_EVENTS = ("UserPromptSubmit", "Stop", "SessionEnd")
PS_TIMEOUT_SECONDS = 1.0
CODEX_IDENTITY_TIMEOUT_SECONDS = 1.5
RECORD_STRING_FIELDS = frozenset(
    {
        "session_id",
        "turn_id",
        "cwd",
        "state",
        "started_at",
        "updated_at",
        "process_start_fingerprint",
    }
)
RECORD_FIELDS = RECORD_STRING_FIELDS | {"schema_version", "pid"}
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
CLAIM_SCHEMA_VERSION = 1
CLAIM_STRING_FIELDS = frozenset({"session_id", "client", "cwd", "label", "created_at"})
CLAIM_FIELDS = CLAIM_STRING_FIELDS | {"schema_version"}
NOTE_SCHEMA_VERSION = 1
NOTE_TTL_SECONDS = 7 * 24 * 60 * 60
NOTE_REQUIRED_STRING_FIELDS = frozenset({"id", "text", "created_at"})
NOTE_OPTIONAL_STRING_FIELDS = frozenset({"session_id", "client"})
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


def _ensure_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _ensure_registry_dir(registry_dir: Path) -> None:
    _ensure_dir(registry_dir)


def _digest_filename(key: str) -> str:
    return f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"


def _record_path(registry_dir: Path, session_id: str) -> Path:
    return registry_dir / _digest_filename(session_id)


def _ps_value(pid: int, field: str, timeout_seconds: float = PS_TIMEOUT_SECONDS) -> str:
    if timeout_seconds <= 0:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def process_start_fingerprint(
    pid: int, timeout_seconds: float = PS_TIMEOUT_SECONDS
) -> str | None:
    """Return a stable fingerprint for one OS process lifetime."""
    started = _ps_value(pid, "lstart", timeout_seconds)
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


def codex_process_identity(
    timeout_seconds: float = CODEX_IDENTITY_TIMEOUT_SECONDS,
) -> ProcessIdentity | None:
    """Find the Codex ancestor that launched this hook."""
    pid = os.getppid()
    visited: set[int] = set()
    deadline = time.monotonic() + timeout_seconds

    def ps_value(field: str) -> str:
        remaining = deadline - time.monotonic()
        return _ps_value(pid, field, min(PS_TIMEOUT_SECONDS, remaining))

    for _ in range(16):
        if pid <= 1 or pid in visited or time.monotonic() >= deadline:
            break
        visited.add(pid)

        command_name = ps_value("comm")
        command = ps_value("command")
        if _looks_like_codex_process(command_name, command):
            fingerprint = process_start_fingerprint(
                pid, min(PS_TIMEOUT_SECONDS, deadline - time.monotonic())
            )
            if fingerprint:
                return pid, fingerprint

        parent = ps_value("ppid")
        try:
            pid = int(parent)
        except ValueError:
            break

    return None


def process_ancestors(
    start_pid: int,
    timeout_seconds: float = CODEX_IDENTITY_TIMEOUT_SECONDS,
    ps_value: Callable[[int, str, float], str] = _ps_value,
) -> list[int]:
    """Return a bounded ancestor pid chain starting at start_pid (inclusive)."""
    pid = start_pid
    visited: set[int] = set()
    deadline = time.monotonic() + timeout_seconds
    chain: list[int] = []

    for _ in range(16):
        if pid <= 1 or pid in visited or time.monotonic() >= deadline:
            break
        visited.add(pid)
        chain.append(pid)
        remaining = deadline - time.monotonic()
        parent = ps_value(pid, "ppid", min(PS_TIMEOUT_SECONDS, remaining))
        try:
            pid = int(parent)
        except ValueError:
            break

    return chain


def resolve_identity(
    codex_home: Path | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    claude_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ancestors: Sequence[int] | None = None,
    start_pid: int | None = None,
    timeout_seconds: float = CODEX_IDENTITY_TIMEOUT_SECONDS,
) -> dict[str, str] | None:
    """Identify the calling session by matching PID ancestry against live sessions."""
    chain = (
        list(ancestors)
        if ancestors is not None
        else process_ancestors(
            start_pid if start_pid is not None else os.getppid(), timeout_seconds
        )
    )
    if not chain:
        return None
    ancestor_pids = set(chain)

    home = codex_home or _codex_home()
    _, codex_sessions = collect_codex_sessions(
        home, fingerprint_lookup=fingerprint_lookup
    )
    for session in codex_sessions:
        if session["pid"] in ancestor_pids:
            return {"client": "codex", "session_id": session["session_id"]}

    _, claude_sessions = collect_claude_sessions(runner=claude_runner)
    for session in claude_sessions:
        if session["pid"] is not None and session["pid"] in ancestor_pids:
            return {"client": "claude", "session_id": session["session_id"]}

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
    if any(
        not isinstance(data.get(field), str) or not data[field]
        for field in RECORD_STRING_FIELDS
    ):
        return None
    if data.get("state") != LIVE_CODEX_STATE:
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return data


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
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


def _remove_record(registry_dir: Path, session_id: str) -> None:
    try:
        _record_path(registry_dir, session_id).unlink()
    except FileNotFoundError:
        pass


def _claims_dir(registry_dir: Path) -> Path:
    return registry_dir / "claims"


def _claim_path(claims_dir: Path, session_id: str) -> Path:
    return claims_dir / _digest_filename(session_id)


def _read_claim(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or set(data) != CLAIM_FIELDS:
        return None
    if data.get("schema_version") != CLAIM_SCHEMA_VERSION:
        return None
    if any(
        not isinstance(data.get(field), str) or not data[field]
        for field in CLAIM_STRING_FIELDS
    ):
        return None
    if data["client"] not in ("codex", "claude"):
        return None
    return data


def write_claim(
    registry_dir: Path,
    session_id: str,
    client: str,
    cwd: str,
    label: str,
    now: str | None = None,
) -> Path:
    """Write (or replace) the one-line task-label sidecar for a session."""
    claims_dir = _claims_dir(registry_dir)
    _ensure_dir(claims_dir)
    path = _claim_path(claims_dir, session_id)
    record = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "session_id": session_id,
        "client": client,
        "cwd": cwd,
        "label": label,
        "created_at": now or _utc_now(),
    }
    _atomic_write_json(path, record)
    return path


def _load_claims(registry_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    claims_dir = _claims_dir(registry_dir)
    if not claims_dir.is_dir():
        return {}
    claims: dict[tuple[str, str], dict[str, Any]] = {}
    for path in claims_dir.glob("*.json"):
        record = _read_claim(path)
        if record is not None:
            claims[(record["client"], record["session_id"])] = record
    return claims


def _notes_dir(registry_dir: Path) -> Path:
    return registry_dir / "notes"


def _notes_path(notes_dir: Path, repo_root: str) -> Path:
    return notes_dir / _digest_filename(repo_root)


def _note_id(text: str, created_at: str) -> str:
    return hashlib.sha256(f"{created_at}\n{text}".encode()).hexdigest()[:8]


def _repo_root(
    cwd: str, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> str:
    try:
        result = runner(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    if result.returncode != 0:
        return cwd
    return result.stdout.strip() or cwd


def _read_notes_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != NOTE_SCHEMA_VERSION:
        return None
    repo_root = data.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root:
        return None
    entries = data.get("notes")
    if not isinstance(entries, list):
        return None
    valid_entries: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if any(
            not isinstance(entry.get(field), str) or not entry[field]
            for field in NOTE_REQUIRED_STRING_FIELDS
        ):
            continue
        if any(
            field in entry and (not isinstance(entry[field], str) or not entry[field])
            for field in NOTE_OPTIONAL_STRING_FIELDS
        ):
            continue
        valid_entries.append(entry)
    return {
        "schema_version": NOTE_SCHEMA_VERSION,
        "repo_root": repo_root,
        "notes": valid_entries,
    }


def add_note(
    registry_dir: Path,
    repo_root: str,
    text: str,
    session_id: str | None = None,
    client: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Append one repo-scoped note; returns the stored entry."""
    notes_dir = _notes_dir(registry_dir)
    _ensure_dir(notes_dir)
    path = _notes_path(notes_dir, repo_root)
    existing = _read_notes_file(path) if path.exists() else None
    entries = list(existing["notes"]) if existing is not None else []
    created_at = now or _utc_now()
    entry: dict[str, Any] = {
        "id": _note_id(text, created_at),
        "text": text,
        "created_at": created_at,
    }
    if session_id:
        entry["session_id"] = session_id
    if client:
        entry["client"] = client
    entries.append(entry)
    _atomic_write_json(
        path,
        {
            "schema_version": NOTE_SCHEMA_VERSION,
            "repo_root": repo_root,
            "notes": entries,
        },
    )
    return entry


def remove_note(registry_dir: Path, repo_root: str, note_id: str) -> bool:
    """Remove one note entry by id; returns whether anything was removed."""
    notes_dir = _notes_dir(registry_dir)
    path = _notes_path(notes_dir, repo_root)
    existing = _read_notes_file(path) if path.exists() else None
    if existing is None:
        return False
    remaining = [entry for entry in existing["notes"] if entry["id"] != note_id]
    if len(remaining) == len(existing["notes"]):
        return False
    if remaining:
        _atomic_write_json(
            path,
            {
                "schema_version": NOTE_SCHEMA_VERSION,
                "repo_root": existing["repo_root"],
                "notes": remaining,
            },
        )
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return True


def load_notes(
    registry_dir: Path, now: str | None = None, stderr: TextIO = sys.stderr
) -> dict[str, list[dict[str, Any]]]:
    """Return unexpired notes per repo root, pruning expired entries from disk."""
    notes_dir = _notes_dir(registry_dir)
    if not notes_dir.is_dir():
        return {}
    timestamp = now or _utc_now()
    result: dict[str, list[dict[str, Any]]] = {}
    for path in notes_dir.glob("*.json"):
        parsed = _read_notes_file(path)
        if parsed is None:
            continue
        kept_raw: list[dict[str, Any]] = []
        kept_with_age: list[dict[str, Any]] = []
        for entry in parsed["notes"]:
            age = _age_seconds(entry["created_at"], timestamp)
            if age is None or age > NOTE_TTL_SECONDS:
                continue
            kept_raw.append(entry)
            kept_with_age.append({**entry, "age_seconds": age})
        if len(kept_raw) != len(parsed["notes"]):
            if kept_raw:
                try:
                    _atomic_write_json(
                        path,
                        {
                            "schema_version": NOTE_SCHEMA_VERSION,
                            "repo_root": parsed["repo_root"],
                            "notes": kept_raw,
                        },
                    )
                except OSError as error:
                    _warn(
                        f"could not prune expired notes in {path.name}: {error}", stderr
                    )
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    _warn(
                        f"could not remove empty notes file {path.name}: {error}",
                        stderr,
                    )
        if kept_with_age:
            result[parsed["repo_root"]] = kept_with_age
    return result


def prune_registry(
    registry_dir: Path,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    stderr: TextIO = sys.stderr,
    exclude_path: Path | None = None,
    claude_session_ids: frozenset[str] | None = None,
) -> None:
    """Remove malformed/dead session records and claims whose session is gone."""
    if registry_dir.is_dir():
        for path in registry_dir.glob("*.json"):
            if path == exclude_path:
                continue
            record = _read_record(path)
            stale = record is None
            if record is not None:
                stale = (
                    fingerprint_lookup(record["pid"])
                    != record["process_start_fingerprint"]
                )
            if not stale:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                _warn(f"could not prune {path.name}: {error}", stderr)

    claims_dir = _claims_dir(registry_dir)
    if claims_dir.is_dir():
        for path in claims_dir.glob("*.json"):
            record = _read_claim(path)
            stale = record is None
            if record is not None:
                if record["client"] == "codex":
                    stale = not _record_path(
                        registry_dir, record["session_id"]
                    ).exists()
                elif claude_session_ids is not None:
                    stale = record["session_id"] not in claude_session_ids
                else:
                    stale = False
            if not stale:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                _warn(f"could not prune claim {path.name}: {error}", stderr)


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

    event = data.get("hook_event_name")
    session_id = data.get("session_id")
    if event not in HOOK_EVENTS:
        raise ValueError("unsupported or missing hook_event_name")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session_id")

    if event in {"Stop", "SessionEnd"}:
        _remove_record(registry_dir, session_id)
        prune_registry(
            registry_dir, fingerprint_lookup=fingerprint_lookup, stderr=stderr
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
    _atomic_write_json(path, record)
    prune_registry(
        registry_dir,
        fingerprint_lookup=fingerprint_lookup,
        stderr=stderr,
        exclude_path=path,
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
            data, _registry_dir(codex_home or _codex_home()), stderr=stderr
        )
    except Exception as error:  # noqa: BLE001 - Hook mode must never block Codex.
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
            if handler.get("type") != "command" or not isinstance(command, str):
                continue
            try:
                command_tokens = shlex.split(command)
            except ValueError:
                continue
            if command_tokens == [script, "hook"]:
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
    provider = {"ok": True, "source": CODEX_SOURCE}
    registration_error = _registration_error(
        home, script_path or Path(__file__).resolve()
    )
    if registration_error:
        return {**provider, "ok": False, "error": registration_error}, []

    registry_dir = _registry_dir(home)
    if not registry_dir.exists():
        return provider, []
    if not registry_dir.is_dir():
        return {
            **provider,
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
                "updated_at": record["updated_at"],
                "name": None,
                "waiting_for": None,
            }
        )

    if invalid_records:
        provider = {
            **provider,
            "ok": False,
            "error": f"{invalid_records} invalid registry record(s)",
        }
    return provider, sessions


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


def collect_claude_sessions(
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

    sessions, errors = normalize_claude_sessions(rows)
    if errors:
        return {**provider, "ok": False, "error": "; ".join(errors)}, sessions
    return provider, sessions


def _provider_failure(
    source: str, error: Exception
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    detail = str(error) or type(error).__name__
    return {"ok": False, "source": source, "error": detail}, []


def _parse_iso8601(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(reference: str, now: str) -> int | None:
    reference_dt = _parse_iso8601(reference)
    now_dt = _parse_iso8601(now)
    if reference_dt is None or now_dt is None:
        return None
    return max(0, int((now_dt - reference_dt).total_seconds()))


def _format_age(seconds: int | None) -> str:
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


def build_inventory(
    codex_home: Path | None = None,
    now: str | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
) -> dict[str, Any]:
    home = codex_home or _codex_home()
    timestamp = now or _utc_now()
    try:
        codex_provider, codex_sessions = collect_codex_sessions(
            home, fingerprint_lookup=fingerprint_lookup
        )
    except Exception as error:  # noqa: BLE001 - Providers fail independently.
        codex_provider, codex_sessions = _provider_failure(CODEX_SOURCE, error)
    try:
        claude_provider, claude_sessions = collect_claude_sessions()
    except Exception as error:  # noqa: BLE001 - Providers fail independently.
        claude_provider, claude_sessions = _provider_failure(CLAUDE_SOURCE, error)

    registry_dir = _registry_dir(home)
    claude_session_ids = (
        frozenset(row["session_id"] for row in claude_sessions)
        if claude_provider["ok"]
        else None
    )
    prune_registry(
        registry_dir,
        fingerprint_lookup=fingerprint_lookup,
        claude_session_ids=claude_session_ids,
    )
    claims = _load_claims(registry_dir)

    sessions = []
    for session in (*codex_sessions, *claude_sessions):
        claim = claims.get((session["client"], session["session_id"]))
        reference = session.get("updated_at") or session["started_at"]
        sessions.append(
            {
                **session,
                "label": claim["label"] if claim else None,
                "age_seconds": _age_seconds(reference, timestamp),
            }
        )

    client_order = {"codex": 0, "claude": 1}
    sessions.sort(
        key=lambda row: (
            client_order.get(row["client"], 99),
            row["started_at"],
            row["session_id"],
        )
    )
    providers = {"codex": codex_provider, "claude": claude_provider}
    notes = load_notes(registry_dir, now=timestamp)
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": all(provider["ok"] for provider in providers.values()),
        "providers": providers,
        "sessions": sessions,
        "notes": notes,
    }


def _human_text(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)[1:-1]


def _human_status(document: dict[str, Any]) -> str:
    lines: list[str] = []
    sessions = document["sessions"]
    if sessions:
        lines.append("CLIENT\tSTATE\tAGE\tNAME/LABEL\tSESSION\tCWD")
        for session in sessions:
            label = session.get("label") or session.get("name") or ""
            lines.append(
                "\t".join(
                    (
                        _human_text(session["client"]),
                        _human_text(session["state"]),
                        _human_text(_format_age(session.get("age_seconds"))),
                        _human_text(label),
                        _human_text(session["session_id"]),
                        _human_text(session["cwd"]),
                    )
                )
            )
    else:
        lines.append("No executing agent sessions.")

    provider_parts = []
    failed = []
    for name, provider in document["providers"].items():
        provider_state = "ok" if provider["ok"] else "unavailable"
        provider_parts.append(
            f"{_human_text(name)}={provider_state} ({_human_text(provider['source'])})"
        )
        if not provider["ok"]:
            failed.append(
                f"{_human_text(name)}: "
                f"{_human_text(provider.get('error', 'unknown error'))}"
            )
    lines.append(f"Coverage: {'; '.join(provider_parts)}")
    if failed:
        lines.append(f"WARNING: incomplete provider coverage — {'; '.join(failed)}")

    notes = document.get("notes") or {}
    for repo_root in sorted(notes):
        entries = notes[repo_root]
        if not entries:
            continue
        lines.append(f"Notes ({_human_text(repo_root)}):")
        for entry in entries:
            lines.append(
                "  ".join(
                    (
                        _human_text(entry["id"]),
                        _human_text(_format_age(entry.get("age_seconds"))),
                        _human_text(entry["text"]),
                    )
                )
            )
    return "\n".join(lines) + "\n"


def run_status(
    json_output: bool,
    stdout: TextIO = sys.stdout,
    codex_home: Path | None = None,
    now: str | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
) -> int:
    document = build_inventory(
        codex_home=codex_home, now=now, fingerprint_lookup=fingerprint_lookup
    )
    if json_output:
        json.dump(document, stdout, indent=2)
        stdout.write("\n")
    else:
        stdout.write(_human_status(document))
    return 0 if document["complete"] else 2


def run_identity(
    codex_home: Path | None = None,
    identity: dict[str, str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    home = codex_home or _codex_home()
    resolved = identity if identity is not None else resolve_identity(codex_home=home)
    if resolved is None:
        _warn("could not resolve the calling session", stderr)
        return 1
    claims_dir = _claims_dir(_registry_dir(home))
    claim_path = _claim_path(claims_dir, resolved["session_id"])
    claim = _read_claim(claim_path) if claim_path.exists() else None
    if claim is not None and claim["client"] != resolved["client"]:
        claim = None
    line = f"client={resolved['client']} session={resolved['session_id']}"
    if claim is not None:
        line += f" label={claim['label']}"
    stdout.write(line + "\n")
    return 0


def run_claim(
    label: str,
    codex_home: Path | None = None,
    client_override: str | None = None,
    session_override: str | None = None,
    cwd: str | None = None,
    now: str | None = None,
    identity: dict[str, str] | None = None,
    stderr: TextIO = sys.stderr,
) -> int:
    home = codex_home or _codex_home()
    if client_override is not None and session_override is not None:
        resolved = {"client": client_override, "session_id": session_override}
    else:
        resolved = (
            identity if identity is not None else resolve_identity(codex_home=home)
        )
    if resolved is None:
        _warn("could not resolve the calling session for claim", stderr)
        return 1
    registry_dir = _registry_dir(home)
    _ensure_registry_dir(registry_dir)
    write_claim(
        registry_dir,
        session_id=resolved["session_id"],
        client=resolved["client"],
        cwd=cwd or os.getcwd(),
        label=label,
        now=now,
    )
    return 0


def run_note(
    text: str | None,
    done_id: str | None,
    codex_home: Path | None = None,
    cwd: str | None = None,
    now: str | None = None,
    identity: dict[str, str] | None = None,
    repo_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    home = codex_home or _codex_home()
    registry_dir = _registry_dir(home)
    root = _repo_root(cwd or os.getcwd(), runner=repo_runner)
    _ensure_registry_dir(registry_dir)

    if done_id is not None:
        if remove_note(registry_dir, root, done_id):
            return 0
        _warn(f"no note {done_id!r} found for {root}", stderr)
        return 1

    resolved = identity if identity is not None else resolve_identity(codex_home=home)
    entry = add_note(
        registry_dir,
        root,
        text or "",
        session_id=resolved["session_id"] if resolved else None,
        client=resolved["client"] if resolved else None,
        now=now,
    )
    stdout.write(f"{entry['id']}\n")
    return 0


def _parse_claim_args(
    args: Sequence[str],
) -> tuple[str, str | None, str | None] | None:
    client_override: str | None = None
    session_override: str | None = None
    positional: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--client" and index + 1 < len(args):
            client_override = args[index + 1]
            index += 2
        elif token == "--session" and index + 1 < len(args):
            session_override = args[index + 1]
            index += 2
        else:
            positional.append(token)
            index += 1
    if len(positional) != 1 or not positional[0]:
        return None
    if (client_override is None) != (session_override is None):
        return None
    if client_override is not None and client_override not in ("codex", "claude"):
        return None
    return positional[0], client_override, session_override


def _parse_note_args(args: Sequence[str]) -> tuple[str | None, str | None] | None:
    if len(args) == 2 and args[0] == "--done":
        return (None, args[1]) if args[1] else None
    if len(args) == 1 and args[0]:
        return args[0], None
    return None


def _usage(stderr: TextIO | None = None) -> int:
    print(
        "usage: agent_session_status.py hook | status [--json] | identity | "
        "claim [--client <client> --session <id>] <label> | "
        "note <text> | note --done <id>",
        file=stderr or sys.stderr,
    )
    return 64


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return _usage()
    verb, rest = arguments[0], arguments[1:]

    if verb == "hook":
        if rest:
            _warn("hook mode ignores command-line arguments")
        return run_hook()
    if verb == "status":
        if rest not in ([], ["--json"]):
            return _usage()
        return run_status(json_output=bool(rest))
    if verb == "identity":
        if rest:
            return _usage()
        return run_identity()
    if verb == "claim":
        parsed = _parse_claim_args(rest)
        if parsed is None:
            return _usage()
        label, client_override, session_override = parsed
        return run_claim(
            label, client_override=client_override, session_override=session_override
        )
    if verb == "note":
        parsed = _parse_note_args(rest)
        if parsed is None:
            return _usage()
        text, done_id = parsed
        return run_note(text=text, done_id=done_id)

    return _usage()


if __name__ == "__main__":
    raise SystemExit(main())
