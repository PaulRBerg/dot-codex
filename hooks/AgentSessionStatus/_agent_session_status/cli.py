"""Command-line commands, argument parsing, and dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from . import codex, inventory, registry
from ._core import codex_home as default_codex_home
from ._core import registry_dir, warn
from .process import FingerprintLookup, process_start_fingerprint

PRESENCE_GIT_TIMEOUT_SECONDS = 2.0
MAX_LABEL_CHARS = 80


def run_status(
    json_output: bool,
    stdout: TextIO = sys.stdout,
    codex_home: Path | None = None,
    now: str | None = None,
    fingerprint_lookup: FingerprintLookup = process_start_fingerprint,
) -> int:
    document = inventory.build_inventory(
        codex_home=codex_home, now=now, fingerprint_lookup=fingerprint_lookup
    )
    if json_output:
        json.dump(document, stdout, indent=2)
        stdout.write("\n")
    else:
        stdout.write(inventory.human_status(document))
    return 0 if document["complete"] else 2


def _repo_root(
    cwd: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Return the git root for cwd, falling back to cwd itself."""
    try:
        result = runner(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=PRESENCE_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    if result.returncode != 0:
        return cwd
    return result.stdout.strip() or cwd


def _path_within(path: Path, root: Path) -> bool:
    """Return whether path is root itself or nested under it."""
    try:
        path, root = path.resolve(), root.resolve()
    except OSError:
        pass
    return path.is_relative_to(root)


def _sanitize_label(text: str) -> str:
    """Flatten cross-session text to one bounded printable line."""
    printable = "".join(char if char.isprintable() else " " for char in text)
    collapsed = " ".join(printable.split())
    if len(collapsed) > MAX_LABEL_CHARS:
        return collapsed[: MAX_LABEL_CHARS - 1].rstrip() + "…"
    return collapsed


def _session_label(session: dict[str, Any]) -> str:
    """Identify a session by claim label or name, else client and short ID."""
    for key in ("label", "name"):
        value = session.get(key)
        if isinstance(value, str):
            sanitized = _sanitize_label(value)
            if sanitized:
                return sanitized
    client = session.get("client", "")
    session_id = session.get("session_id", "")
    return f"{client}/{session_id[:8]}"


def build_presence_line(
    document: dict[str, Any], repo_root: str, own_session_id: str
) -> str:
    """Return the one-line presence notice, or "" when nothing is pending."""
    root_path = Path(repo_root)
    others: list[str] = []
    for session in document.get("sessions", []):
        if not isinstance(session, dict):
            continue
        session_id = session.get("session_id")
        cwd = session.get("cwd")
        if session_id == own_session_id:
            continue
        if not isinstance(cwd, str) or not cwd:
            continue
        if not _path_within(Path(cwd), root_path):
            continue
        others.append(_session_label(session))

    note_count = 0
    notes = document.get("notes")
    if isinstance(notes, dict):
        entries = notes.get(repo_root)
        if isinstance(entries, list):
            note_count = len(entries)

    parts: list[str] = []
    if others:
        word = "session" if len(others) == 1 else "sessions"
        parts.append(f"{len(others)} other {word} in this repo ({', '.join(others)})")
    if note_count:
        word = "note" if note_count == 1 else "notes"
        parts.append(f"{note_count} {word} pending — run agents-status")
    return f"agents: {'; '.join(parts)}" if parts else ""


def run_presence(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    inventory_builder: Callable[[], dict[str, Any]] | None = None,
    repo_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Print a safe, one-line notice of other agents in the caller's repo."""
    line = ""
    try:
        payload = json.load(stdin)
        if not isinstance(payload, dict):
            raise TypeError("hook input must be a JSON object")
        session_id = payload.get("session_id")
        cwd = payload.get("cwd")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("missing session_id")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("missing cwd")

        document = (
            inventory_builder()
            if inventory_builder is not None
            else inventory.build_inventory(stderr=StringIO())
        )
        if document.get("complete"):
            line = build_presence_line(
                document, _repo_root(cwd, runner=repo_runner), session_id
            )
    except Exception:  # noqa: BLE001 - A hook must never break a prompt.
        line = ""
    if line:
        try:
            stdout.write(line + "\n")
        except OSError:
            pass
    return 0


def run_identity(
    codex_home: Path | None = None,
    identity: dict[str, str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    home = codex_home or default_codex_home()
    resolved = (
        identity
        if identity is not None
        else inventory.resolve_identity(codex_home=home)
    )
    if resolved is None:
        warn("could not resolve the calling session", stderr)
        return 1
    claims = registry.claims_dir(registry_dir(home))
    path = registry.claim_path(claims, resolved["session_id"])
    claim = registry.read_claim(path)
    if claim is not None and claim["client"] != resolved["client"]:
        claim = None
    line = (
        f"client={inventory.human_text(resolved['client'])} "
        f"session={inventory.human_text(resolved['session_id'])}"
    )
    if claim is not None:
        line += f" label={inventory.human_text(claim['label'])}"
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
    home = codex_home or default_codex_home()
    if client_override is not None and session_override is not None:
        resolved = {"client": client_override, "session_id": session_override}
    else:
        resolved = (
            identity
            if identity is not None
            else inventory.resolve_identity(codex_home=home)
        )
    if resolved is None:
        warn("could not resolve the calling session for claim", stderr)
        return 1
    storage = registry_dir(home)
    registry.ensure_registry_dir(storage)
    registry.write_claim(
        storage,
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
    home = codex_home or default_codex_home()
    storage = registry_dir(home)
    root = registry.repo_root(cwd or os.getcwd(), runner=repo_runner)
    registry.ensure_registry_dir(storage)

    if done_id is not None:
        if registry.remove_note(storage, root, done_id):
            return 0
        warn(f"no note {done_id!r} found for {root}", stderr)
        return 1

    resolved = (
        identity
        if identity is not None
        else inventory.resolve_identity(codex_home=home)
    )
    entry = registry.add_note(
        storage,
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
    if session_override is not None and not session_override:
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
        "presence | "
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
            warn("hook mode ignores command-line arguments")
        return codex.run_hook()
    if verb == "status":
        if rest not in ([], ["--json"]):
            return _usage()
        return run_status(json_output=bool(rest))
    if verb == "presence":
        if rest:
            return _usage()
        return run_presence()
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
