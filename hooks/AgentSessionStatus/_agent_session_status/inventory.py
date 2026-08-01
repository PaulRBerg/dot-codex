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
    return {"ok": False, "source": source, "error": detail}, []


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
            home, fingerprint_lookup=fingerprint_lookup
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
    providers = {"codex": codex_provider, "claude": claude_provider}
    notes = registry.load_notes(storage, now=timestamp, stderr=stderr)
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": all(provider["ok"] for provider in providers.values()),
        "providers": providers,
        "sessions": sessions,
        "notes": notes,
    }


def human_text(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)[1:-1]


def human_status(document: dict[str, Any]) -> str:
    lines: list[str] = []
    sessions = document["sessions"]
    if sessions:
        lines.append("CLIENT\tSTATE\tAGE\tNAME/LABEL\tSESSION\tCWD")
        for session in sessions:
            label = session.get("label") or session.get("name") or ""
            lines.append(
                "\t".join(
                    (
                        human_text(session["client"]),
                        human_text(session["state"]),
                        human_text(format_age(session.get("age_seconds"))),
                        human_text(label),
                        human_text(session["session_id"]),
                        human_text(session["cwd"]),
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
            f"{human_text(name)}={provider_state} ({human_text(provider['source'])})"
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
    for repo_root in sorted(notes):
        entries = notes[repo_root]
        if not entries:
            continue
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
    return "\n".join(lines) + "\n"
