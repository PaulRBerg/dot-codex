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
    LIVE_CODEX_STATE,
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
CLAIM_SCHEMA_VERSION = 1
CLAIM_STRING_FIELDS = frozenset({"session_id", "client", "cwd", "label", "created_at"})
CLAIM_FIELDS = CLAIM_STRING_FIELDS | {"schema_version"}
NOTE_SCHEMA_VERSION = 1
NOTE_TTL_SECONDS = 7 * 24 * 60 * 60
NOTE_REQUIRED_STRING_FIELDS = frozenset({"id", "text", "created_at"})
NOTE_OPTIONAL_STRING_FIELDS = frozenset({"session_id", "client"})


def ensure_registry_dir(registry_dir: Path) -> None:
    ensure_dir(registry_dir)


def record_path(registry_dir: Path, session_id: str) -> Path:
    return registry_dir / digest_filename(session_id)


def read_record(path: Path) -> dict[str, Any] | None:
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
    claims = claims_dir(registry_dir)
    ensure_dir(claims)
    path = claim_path(claims, session_id)
    record = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "session_id": session_id,
        "client": client,
        "cwd": cwd,
        "label": label,
        "created_at": now or utc_now(),
    }
    atomic_write_json(path, record)
    return path


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
            record = read_record(path)
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
                    stale = not record_path(registry_dir, record["session_id"]).exists()
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
