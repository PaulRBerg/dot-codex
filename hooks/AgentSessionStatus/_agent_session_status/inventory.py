"""Cross-client identity resolution, inventory assembly, and rendering."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import claude, codex, process, registry
from ._core import (
    CLAUDE_SOURCE,
    CODEX_SOURCE,
    SCHEMA_VERSION,
    age_seconds,
    format_age,
    registry_dir,
    utc_now,
)
from ._core import (
    codex_home as default_codex_home,
)
from .process import FingerprintLookup, process_start_fingerprint


def resolve_identity(
    codex_home: Path | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    claude_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ancestors: Sequence[int] | None = None,
    start_pid: int | None = None,
    timeout_seconds: float = process.CODEX_IDENTITY_TIMEOUT_SECONDS,
) -> dict[str, str] | None:
    """Identify the calling session by matching PID ancestry against live sessions."""
    chain = (
        list(ancestors)
        if ancestors is not None
        else process.process_ancestors(
            start_pid if start_pid is not None else os.getppid(), timeout_seconds
        )
    )
    if not chain:
        return None
    ancestor_pids = set(chain)

    home = codex_home or default_codex_home()
    _, codex_sessions = codex.collect_sessions(
        home, fingerprint_lookup=fingerprint_lookup
    )
    for session in codex_sessions:
        if session["pid"] in ancestor_pids:
            return {"client": "codex", "session_id": session["session_id"]}

    _, claude_sessions = claude.collect_sessions(runner=claude_runner)
    for session in claude_sessions:
        if session["pid"] is not None and session["pid"] in ancestor_pids:
            return {"client": "claude", "session_id": session["session_id"]}

    return None


def _provider_failure(
    source: str, error: Exception
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    detail = str(error) or type(error).__name__
    return {"ok": False, "source": source, "dropped": 0, "error": detail}, []


def build_inventory(
    codex_home: Path | None = None,
    now: str | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
    stderr: TextIO = sys.stderr,
) -> dict[str, Any]:
    home = codex_home or default_codex_home()
    timestamp = now or utc_now()
    try:
        codex_provider, codex_sessions = codex.collect_sessions(
            home, fingerprint_lookup=fingerprint_lookup, now=timestamp
        )
    except Exception as error:  # noqa: BLE001 - Providers fail independently.
        codex_provider, codex_sessions = _provider_failure(CODEX_SOURCE, error)
    try:
        claude_provider, claude_sessions = claude.collect_sessions()
    except Exception as error:  # noqa: BLE001 - Providers fail independently.
        claude_provider, claude_sessions = _provider_failure(CLAUDE_SOURCE, error)

    storage = registry_dir(home)
    claude_session_ids = (
        frozenset(row["session_id"] for row in claude_sessions)
        if claude_provider["ok"]
        else None
    )
    registry.prune_registry(
        storage,
        fingerprint_lookup=fingerprint_lookup,
        claude_session_ids=claude_session_ids,
        stderr=stderr,
        now=timestamp,
    )
    claims = registry.load_claims(storage)

    sessions = []
    for session in (*codex_sessions, *claude_sessions):
        claim = claims.get((session["client"], session["session_id"]))
        reference = session.get("updated_at") or session["started_at"]
        sessions.append(
            {
                **session,
                "label": claim["label"] if claim else None,
                "paths": claim["paths"] if claim else [],
                "age_seconds": age_seconds(reference, timestamp),
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
    providers = {
        "codex": {**codex_provider, "dropped": codex_provider.get("dropped", 0)},
        "claude": {
            **claude_provider,
            "dropped": claude_provider.get("dropped", 0),
        },
    }
    notes = registry.load_notes(storage, now=timestamp, stderr=stderr)
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": all(provider["ok"] for provider in providers.values())
        and sum(provider["dropped"] for provider in providers.values()) == 0,
        "providers": providers,
        "sessions": sessions,
        "notes": notes,
    }


def human_text(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)[1:-1]


def _sanitize_detail(value: str) -> str:
    printable = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(printable.split())


def _cap_detail(value: str, limit: int = 60) -> str:
    if len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def human_status(document: dict[str, Any]) -> str:
    lines: list[str] = []
    sessions = document["sessions"]
    if sessions:
        lines.append("CLIENT\tSTATE\tAGE\tNAME/LABEL\tSESSION\tCWD\tDETAIL")
        for session in sessions:
            label = session.get("label") or session.get("name") or ""
            details: list[str] = []
            waiting_for = session.get("waiting_for")
            if session.get("state") == "waiting" and isinstance(waiting_for, str):
                sanitized = _sanitize_detail(waiting_for)
                if sanitized:
                    details.append(f"waiting={sanitized}")
            paths = session.get("paths")
            if isinstance(paths, list) and paths:
                sanitized_paths = [
                    _sanitize_detail(spec) for spec in paths if isinstance(spec, str)
                ]
                details.append(f"paths={','.join(sanitized_paths)}")
            detail = _cap_detail(" ".join(details))
            lines.append(
                "\t".join(
                    (
                        human_text(session["client"]),
                        human_text(session["state"]),
                        human_text(format_age(session.get("age_seconds"))),
                        human_text(label),
                        human_text(session["session_id"]),
                        human_text(session["cwd"]),
                        human_text(detail),
                    )
                )
            )
    else:
        lines.append("No executing agent sessions.")

    provider_parts = []
    failed = []
    for name, provider in document["providers"].items():
        provider_state = "ok" if provider["ok"] else "unavailable"
        dropped = provider.get("dropped", 0)
        dropped_detail = f" [{dropped} dropped]" if dropped else ""
        provider_parts.append(
            f"{human_text(name)}={provider_state} "
            f"({human_text(provider['source'])}{dropped_detail})"
        )
        if not provider["ok"]:
            failed.append(
                f"{human_text(name)}: "
                f"{human_text(provider.get('error', 'unknown error'))}"
            )
    lines.append(f"Coverage: {'; '.join(provider_parts)}")
    if failed:
        lines.append(f"WARNING: incomplete provider coverage — {'; '.join(failed)}")

    notes = document.get("notes") or {}
    rendered_notes = False
    for repo_root in sorted(notes):
        entries = notes[repo_root]
        if not entries:
            continue
        rendered_notes = True
        lines.append(f"Notes ({human_text(repo_root)}):")
        for entry in entries:
            lines.append(
                "  ".join(
                    (
                        human_text(entry["id"]),
                        human_text(format_age(entry.get("age_seconds"))),
                        human_text(entry["text"]),
                    )
                )
            )
    if rendered_notes:
        lines.append("(note --done <id> closes a note)")
    return "\n".join(lines) + "\n"
