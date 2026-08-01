"""Persistent Codex session records, claims, and repository notes."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from ._core import (
    IDLE_CODEX_STATE,
    IDLE_TTL_SECONDS,
    LIVE_CODEX_STATES,
    SCHEMA_VERSION,
    age_seconds,
    atomic_write_json,
    digest_filename,
    ensure_dir,
    utc_now,
    warn,
)
from .process import FingerprintLookup, process_start_fingerprint

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
CLAIM_SCHEMA_VERSION = 2
CLAIM_V1_STRING_FIELDS = frozenset(
    {"session_id", "client", "cwd", "label", "created_at"}
)
CLAIM_V1_FIELDS = CLAIM_V1_STRING_FIELDS | {"schema_version"}
CLAIM_V2_STRING_FIELDS = CLAIM_V1_STRING_FIELDS | {"repo_root"}
CLAIM_V2_FIELDS = CLAIM_V2_STRING_FIELDS | {"schema_version", "paths"}
MAX_SPEC_CHARS = 120
NOTE_SCHEMA_VERSION = 1
NOTE_TTL_SECONDS = 7 * 24 * 60 * 60
NOTE_REQUIRED_STRING_FIELDS = frozenset({"id", "text", "created_at"})
NOTE_OPTIONAL_STRING_FIELDS = frozenset({"session_id", "client"})
INBOX_SCHEMA_VERSION = 1
MESSAGE_TTL_SECONDS = 48 * 3600
MAX_INBOX_MESSAGES = 50
MAX_MESSAGE_CHARS = 240
MAX_LABEL_CHARS = 80
MESSAGE_REQUIRED_STRING_FIELDS = frozenset(
    {"id", "from_client", "from_session_id", "text", "created_at"}
)
MESSAGE_OPTIONAL_STRING_FIELDS = frozenset({"from_label", "repo_root"})


def ensure_registry_dir(registry_dir: Path) -> None:
    ensure_dir(registry_dir)


def record_path(registry_dir: Path, session_id: str) -> Path:
    return registry_dir / digest_filename(session_id)


def read_record(path: Path, now: str | None = None) -> dict[str, Any] | None:
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
    if data.get("state") not in LIVE_CODEX_STATES:
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if data["state"] == IDLE_CODEX_STATE:
        age = age_seconds(data["updated_at"], now or utc_now())
        if age is None or age > IDLE_TTL_SECONDS:
            return None
    return data


def remove_record(registry_dir: Path, session_id: str) -> None:
    try:
        record_path(registry_dir, session_id).unlink()
    except FileNotFoundError:
        pass


def claims_dir(registry_dir: Path) -> Path:
    return registry_dir / "claims"


def claim_path(claims: Path, session_id: str) -> Path:
    return claims / digest_filename(session_id)


def read_claim(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    schema_version = data.get("schema_version")
    if schema_version == 1 and set(data) == CLAIM_V1_FIELDS:
        string_fields = CLAIM_V1_STRING_FIELDS
    elif schema_version == CLAIM_SCHEMA_VERSION and set(data) == CLAIM_V2_FIELDS:
        string_fields = CLAIM_V2_STRING_FIELDS
    else:
        return None
    if any(
        not isinstance(data.get(field), str) or not data[field]
        for field in string_fields
    ):
        return None
    if data["client"] not in ("codex", "claude"):
        return None
    if schema_version == 1:
        return {**data, "repo_root": None, "paths": []}
    paths = data.get("paths")
    if not isinstance(paths, list) or any(
        not isinstance(spec, str) or not spec for spec in paths
    ):
        return None
    return data


def write_claim(
    registry_dir: Path,
    session_id: str,
    client: str,
    cwd: str,
    label: str,
    repo_root: str,
    paths: list[str],
    now: str | None = None,
) -> Path:
    """Write (or replace) the one-line task-label sidecar for a session."""
    claims = claims_dir(registry_dir)
    ensure_dir(claims)
    path = claim_path(claims, session_id)
    record = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "session_id": session_id,
        "client": client,
        "cwd": cwd,
        "label": label,
        "repo_root": repo_root,
        "paths": paths,
        "created_at": now or utc_now(),
    }
    atomic_write_json(path, record)
    return path


def remove_claim(registry_dir: Path, session_id: str) -> bool:
    try:
        claim_path(claims_dir(registry_dir), session_id).unlink()
    except FileNotFoundError:
        return False
    return True


def _sanitize_text(text: str, limit: int) -> str:
    printable = "".join(char if char.isprintable() else " " for char in text)
    collapsed = " ".join(printable.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def normalize_spec(spec: str, cwd: str, repo_root: str) -> str | None:
    try:
        candidate = Path(spec).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        resolved = candidate.resolve()
        root = Path(repo_root).expanduser().resolve()
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    normalized = relative.as_posix().removeprefix("./").rstrip("/") or "."
    sanitized = _sanitize_text(normalized, MAX_SPEC_CHARS)
    return sanitized or None


def load_claims(registry_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    claims = claims_dir(registry_dir)
    if not claims.is_dir():
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for path in claims.glob("*.json"):
        record = read_claim(path)
        if record is not None:
            result[(record["client"], record["session_id"])] = record
    return result


def notes_dir(registry_dir: Path) -> Path:
    return registry_dir / "notes"


def notes_path(notes: Path, repo_root: str) -> Path:
    return notes / digest_filename(repo_root)


def note_id() -> str:
    return os.urandom(4).hex()


@contextmanager
def lock_notes_file(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.stem}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def repo_root(
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
    root = result.stdout.removesuffix("\n").removesuffix("\r")
    return root or cwd


def read_notes_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != NOTE_SCHEMA_VERSION:
        return None
    root = data.get("repo_root")
    if not isinstance(root, str) or not root:
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
        "repo_root": root,
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
    notes = notes_dir(registry_dir)
    ensure_dir(notes)
    path = notes_path(notes, repo_root)
    with lock_notes_file(path):
        existing = read_notes_file(path)
        entries = list(existing["notes"]) if existing is not None else []
        existing_ids = {existing_entry["id"] for existing_entry in entries}
        new_note_id = note_id()
        while new_note_id in existing_ids:
            new_note_id = note_id()
        entry: dict[str, Any] = {
            "id": new_note_id,
            "text": text,
            "created_at": now or utc_now(),
        }
        if session_id:
            entry["session_id"] = session_id
        if client:
            entry["client"] = client
        entries.append(entry)
        atomic_write_json(
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
    notes = notes_dir(registry_dir)
    if not notes.is_dir():
        return False
    path = notes_path(notes, repo_root)
    with lock_notes_file(path):
        existing = read_notes_file(path)
        if existing is None:
            return False
        remaining = [entry for entry in existing["notes"] if entry["id"] != note_id]
        if len(remaining) == len(existing["notes"]):
            return False
        if remaining:
            atomic_write_json(
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
    notes = notes_dir(registry_dir)
    if not notes.is_dir():
        return {}
    timestamp = now or utc_now()
    result: dict[str, list[dict[str, Any]]] = {}
    for path in notes.glob("*.json"):
        with lock_notes_file(path):
            parsed = read_notes_file(path)
            if parsed is None:
                continue
            kept_raw: list[dict[str, Any]] = []
            kept_with_age: list[dict[str, Any]] = []
            for entry in parsed["notes"]:
                age = age_seconds(entry["created_at"], timestamp)
                if age is None or age > NOTE_TTL_SECONDS:
                    continue
                kept_raw.append(entry)
                kept_with_age.append({**entry, "age_seconds": age})
            if len(kept_raw) != len(parsed["notes"]):
                if kept_raw:
                    try:
                        atomic_write_json(
                            path,
                            {
                                "schema_version": NOTE_SCHEMA_VERSION,
                                "repo_root": parsed["repo_root"],
                                "notes": kept_raw,
                            },
                        )
                    except OSError as error:
                        warn(
                            f"could not prune expired notes in {path.name}: {error}",
                            stderr,
                        )
                else:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        warn(
                            f"could not remove empty notes file {path.name}: {error}",
                            stderr,
                        )
            if kept_with_age:
                result[parsed["repo_root"]] = kept_with_age
    return result


def inbox_dir(registry_dir: Path) -> Path:
    return registry_dir / "inbox"


def inbox_path(inbox: Path, client: str, session_id: str) -> Path:
    return inbox / digest_filename(f"{client}:{session_id}")


def read_inbox_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "client",
        "session_id",
        "messages",
    }:
        return None
    if data.get("schema_version") != INBOX_SCHEMA_VERSION:
        return None
    client = data.get("client")
    session_id = data.get("session_id")
    if client not in ("codex", "claude"):
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    entries = data.get("messages")
    if not isinstance(entries, list):
        return None
    valid_entries: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if any(
            not isinstance(entry.get(field), str) or not entry[field]
            for field in MESSAGE_REQUIRED_STRING_FIELDS
        ):
            continue
        if entry["from_client"] not in ("codex", "claude"):
            continue
        if any(
            field in entry and (not isinstance(entry[field], str) or not entry[field])
            for field in MESSAGE_OPTIONAL_STRING_FIELDS
        ):
            continue
        valid_entries.append(entry)
    return {
        "schema_version": INBOX_SCHEMA_VERSION,
        "client": client,
        "session_id": session_id,
        "messages": valid_entries,
    }


def add_message(
    registry_dir: Path,
    to_client: str,
    to_session_id: str,
    message: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any]:
    if to_client not in ("codex", "claude"):
        raise ValueError("invalid recipient client")
    if not isinstance(to_session_id, str) or not to_session_id:
        raise ValueError("missing recipient session ID")
    from_client = message.get("from_client")
    from_session_id = message.get("from_session_id")
    raw_text = message.get("text")
    if from_client not in ("codex", "claude"):
        raise ValueError("invalid sender client")
    if not isinstance(from_session_id, str) or not from_session_id:
        raise ValueError("missing sender session ID")
    if not isinstance(raw_text, str):
        raise ValueError("missing message text")
    sanitized_text = _sanitize_text(raw_text, MAX_MESSAGE_CHARS)
    if not sanitized_text:
        raise ValueError("empty message text")

    inbox = inbox_dir(registry_dir)
    ensure_dir(inbox)
    path = inbox_path(inbox, to_client, to_session_id)
    with lock_notes_file(path):
        existing = read_inbox_file(path)
        entries = list(existing["messages"]) if existing is not None else []
        existing_ids = {existing_entry["id"] for existing_entry in entries}
        new_message_id = note_id()
        while new_message_id in existing_ids:
            new_message_id = note_id()
        entry: dict[str, Any] = {
            "id": new_message_id,
            "from_client": from_client,
            "from_session_id": from_session_id,
            "text": sanitized_text,
            "created_at": now or utc_now(),
        }
        raw_label = message.get("from_label")
        if isinstance(raw_label, str):
            label = _sanitize_text(raw_label, MAX_LABEL_CHARS)
            if label:
                entry["from_label"] = label
        raw_repo_root = message.get("repo_root")
        if isinstance(raw_repo_root, str) and raw_repo_root:
            entry["repo_root"] = raw_repo_root
        entries.append(entry)
        entries = entries[-MAX_INBOX_MESSAGES:]
        atomic_write_json(
            path,
            {
                "schema_version": INBOX_SCHEMA_VERSION,
                "client": to_client,
                "session_id": to_session_id,
                "messages": entries,
            },
        )
    return entry


def load_inbox(
    registry_dir: Path,
    client: str,
    session_id: str,
    now: str | None = None,
) -> list[dict[str, Any]]:
    inbox = inbox_dir(registry_dir)
    if not inbox.is_dir():
        return []
    path = inbox_path(inbox, client, session_id)
    timestamp = now or utc_now()
    with lock_notes_file(path):
        parsed = read_inbox_file(path)
        if parsed is None:
            return []
        kept_raw: list[dict[str, Any]] = []
        kept_with_age: list[dict[str, Any]] = []
        for entry in parsed["messages"]:
            age = age_seconds(entry["created_at"], timestamp)
            if age is None or age > MESSAGE_TTL_SECONDS:
                continue
            kept_raw.append(entry)
            kept_with_age.append({**entry, "age_seconds": age})
        if len(kept_raw) != len(parsed["messages"]):
            if kept_raw:
                atomic_write_json(
                    path,
                    {
                        "schema_version": INBOX_SCHEMA_VERSION,
                        "client": parsed["client"],
                        "session_id": parsed["session_id"],
                        "messages": kept_raw,
                    },
                )
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        return kept_with_age


def ack_messages(
    registry_dir: Path,
    client: str,
    session_id: str,
    ids: list[str] | None,
) -> int:
    inbox = inbox_dir(registry_dir)
    if not inbox.is_dir():
        return 0
    path = inbox_path(inbox, client, session_id)
    with lock_notes_file(path):
        parsed = read_inbox_file(path)
        if parsed is None:
            return 0
        selected = None if ids is None else set(ids)
        remaining = [
            entry
            for entry in parsed["messages"]
            if selected is not None and entry["id"] not in selected
        ]
        removed = len(parsed["messages"]) - len(remaining)
        if not removed:
            return 0
        if remaining:
            atomic_write_json(
                path,
                {
                    "schema_version": INBOX_SCHEMA_VERSION,
                    "client": parsed["client"],
                    "session_id": parsed["session_id"],
                    "messages": remaining,
                },
            )
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return removed


def count_pending(
    registry_dir: Path, session_id: str, now: str | None = None
) -> int:
    return sum(
        len(load_inbox(registry_dir, client, session_id, now=now))
        for client in ("codex", "claude")
    )


def prune_registry(
    registry_dir: Path,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    stderr: TextIO = sys.stderr,
    exclude_path: Path | None = None,
    claude_session_ids: frozenset[str] | None = None,
    now: str | None = None,
) -> None:
    """Remove malformed/dead session state whose recipient is gone."""
    timestamp = now or utc_now()
    if registry_dir.is_dir():
        for path in registry_dir.glob("*.json"):
            if path == exclude_path:
                continue
            record = read_record(path, now=timestamp)
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
                warn(f"could not prune {path.name}: {error}", stderr)

    claims = claims_dir(registry_dir)
    if claims.is_dir():
        for path in claims.glob("*.json"):
            record = read_claim(path)
            stale = record is None
            if record is not None:
                if record["client"] == "codex":
                    stale = (
                        read_record(
                            record_path(registry_dir, record["session_id"]),
                            now=timestamp,
                        )
                        is None
                    )
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
                warn(f"could not prune claim {path.name}: {error}", stderr)

    inbox = inbox_dir(registry_dir)
    if inbox.is_dir():
        for path in inbox.glob("*.json"):
            record = read_inbox_file(path)
            stale = record is None
            if record is not None:
                if record["client"] == "codex":
                    stale = (
                        read_record(
                            record_path(registry_dir, record["session_id"]),
                            now=timestamp,
                        )
                        is None
                    )
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
                warn(f"could not prune inbox {path.name}: {error}", stderr)
