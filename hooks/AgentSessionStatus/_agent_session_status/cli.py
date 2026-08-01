"""Command-line commands, argument parsing, and dispatch."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from . import codex, coordination, inventory, registry
from ._core import codex_home as default_codex_home
from ._core import registry_dir, warn
from .process import FingerprintLookup, process_start_fingerprint

PRESENCE_GIT_TIMEOUT_SECONDS = 2.0
MAX_LABEL_CHARS = 80

run_conflicts = coordination.run_conflicts
run_msg = coordination.run_msg
run_inbox = coordination.run_inbox
run_watch = coordination.run_watch


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


def _sanitize_text(text: str, limit: int = MAX_LABEL_CHARS) -> str:
    """Flatten cross-session text to one bounded printable line."""
    printable = "".join(char if char.isprintable() else " " for char in text)
    collapsed = " ".join(printable.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def _sanitize_label(text: str) -> str:
    return _sanitize_text(text)


def _session_label(
    session: dict[str, Any], limit: int = MAX_LABEL_CHARS
) -> str:
    """Identify a session by claim label or name, else client and short ID."""
    for key in ("label", "name"):
        value = session.get(key)
        if isinstance(value, str):
            sanitized = _sanitize_text(value, limit)
            if sanitized:
                return sanitized
    client = session.get("client", "")
    session_id = session.get("session_id", "")
    return _sanitize_text(f"{client}/{session_id[:8]}", limit)


def build_presence_line(
    document: dict[str, Any],
    repo_root: str,
    own_session_id: str,
    pending_count: int = 0,
    own_claim_exists: bool | None = None,
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
        others.append(_session_label(session, limit=32))

    if own_claim_exists is None:
        own_claim_exists = any(
            isinstance(session, dict)
            and session.get("session_id") == own_session_id
            and isinstance(session.get("label"), str)
            and bool(session["label"])
            for session in document.get("sessions", [])
        )

    note_count = 0
    notes = document.get("notes")
    if isinstance(notes, dict):
        entries = notes.get(repo_root)
        if isinstance(entries, list):
            note_count = len(entries)

    def render(include_others_detail: bool, include_nudge: bool) -> str:
        parts: list[str] = []
        if not document.get("complete"):
            parts.append(
                "coverage incomplete — run agents-status before assuming no conflicts"
            )
        if others:
            word = "session" if len(others) == 1 else "sessions"
            others_part = f"{len(others)} other {word} in this repo"
            if include_others_detail:
                listed = others[:3]
                if len(others) > len(listed):
                    listed.append(f"+{len(others) - len(listed)} more")
                others_part += f" ({', '.join(listed)})"
            parts.append(others_part)
        if pending_count:
            word = "message" if pending_count == 1 else "messages"
            parts.append(f"{pending_count} {word} pending — run inbox")
        if note_count:
            word = "note" if note_count == 1 else "notes"
            parts.append(f"{note_count} {word} pending — run agents-status")
        if others and not own_claim_exists and include_nudge:
            parts.append('no claim set — claim "<label>"')
        return f"agents: {'; '.join(parts)}" if parts else ""

    line = render(include_others_detail=True, include_nudge=True)
    if len(line) > 200:
        line = render(include_others_detail=False, include_nudge=True)
    if len(line) > 200:
        line = render(include_others_detail=False, include_nudge=False)
    if len(line) > 200:
        line = line[:199] + "…"
    return line


def run_presence(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    inventory_builder: Callable[[], dict[str, Any]] | None = None,
    repo_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    codex_home: Path | None = None,
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
        home = codex_home or default_codex_home()
        storage = registry_dir(home)
        claim = registry.read_claim(
            registry.claim_path(registry.claims_dir(storage), session_id)
        )
        line = build_presence_line(
            document,
            _repo_root(cwd, runner=repo_runner),
            session_id,
            pending_count=registry.count_pending(storage, session_id),
            own_claim_exists=claim is not None
            and claim["session_id"] == session_id,
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
    label: str | None,
    codex_home: Path | None = None,
    client_override: str | None = None,
    session_override: str | None = None,
    paths: Sequence[str] = (),
    done: bool = False,
    cwd: str | None = None,
    now: str | None = None,
    identity: dict[str, str] | None = None,
    repo_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    stderr: TextIO = sys.stderr,
) -> int:
    if (done and (label is not None or paths)) or (
        not done and (not isinstance(label, str) or not label)
    ):
        return os.EX_USAGE
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
    if done:
        registry.remove_claim(storage, resolved["session_id"])
        return 0

    current_cwd = cwd or os.getcwd()
    root = registry.repo_root(current_cwd, runner=repo_runner)
    normalized_paths: list[str] = []
    for spec in paths:
        normalized = registry.normalize_spec(spec, current_cwd, root)
        if normalized is None:
            warn(
                f"claim path {_sanitize_text(spec)!r} is outside repo root {root}",
                stderr,
            )
            return os.EX_USAGE
        normalized_paths.append(normalized)
    registry.write_claim(
        storage,
        session_id=resolved["session_id"],
        client=resolved["client"],
        cwd=current_cwd,
        label=label or "",
        repo_root=root,
        paths=normalized_paths,
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
) -> tuple[str | None, bool, str | None, str | None, list[str]] | None:
    client_override: str | None = None
    session_override: str | None = None
    paths: list[str] = []
    done = False
    positional: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if (
            token == "--client"
            and client_override is None
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
        ):
            client_override = args[index + 1]
            index += 2
        elif (
            token == "--session"
            and session_override is None
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
        ):
            session_override = args[index + 1]
            index += 2
        elif (
            token == "--paths"
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
        ):
            spec = args[index + 1]
            if not spec:
                return None
            paths.append(spec)
            index += 2
        elif token == "--done" and not done:
            done = True
            index += 1
        elif token.startswith("--"):
            return None
        else:
            positional.append(token)
            index += 1
    if done:
        if positional or paths:
            return None
        label = None
    elif len(positional) == 1 and positional[0]:
        label = positional[0]
    else:
        return None
    if (client_override is None) != (session_override is None):
        return None
    if client_override is not None and client_override not in ("codex", "claude"):
        return None
    if session_override is not None and not session_override:
        return None
    return label, done, client_override, session_override, paths


def _parse_note_args(args: Sequence[str]) -> tuple[str | None, str | None] | None:
    if len(args) == 2 and args[0] == "--done":
        return (None, args[1]) if args[1] else None
    if len(args) == 1 and args[0]:
        return args[0], None
    return None


def _parse_conflicts_args(args: Sequence[str]) -> list[str] | None:
    paths: list[str] = []
    index = 0
    while index < len(args):
        if (
            args[index] == "--paths"
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
            and args[index + 1]
        ):
            paths.append(args[index + 1])
            index += 2
        else:
            return None
    return paths


def _parse_msg_args(
    args: Sequence[str],
) -> tuple[str, str, str | None, str | None] | None:
    client_override: str | None = None
    session_override: str | None = None
    positional: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if (
            token == "--client"
            and client_override is None
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
            and args[index + 1]
        ):
            client_override = args[index + 1]
            index += 2
        elif (
            token == "--session"
            and session_override is None
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
            and args[index + 1]
        ):
            session_override = args[index + 1]
            index += 2
        elif token.startswith("--"):
            return None
        else:
            positional.append(token)
            index += 1
    if len(positional) != 2 or not all(positional):
        return None
    if (client_override is None) != (session_override is None):
        return None
    if client_override is not None and client_override not in ("codex", "claude"):
        return None
    return positional[0], positional[1], client_override, session_override


def _parse_inbox_args(
    args: Sequence[str],
) -> tuple[str | None, bool, str | None, str | None] | None:
    ack_id: str | None = None
    ack_all = False
    client_override: str | None = None
    session_override: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if (
            token == "--ack"
            and ack_id is None
            and not ack_all
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
            and args[index + 1]
        ):
            ack_id = args[index + 1]
            index += 2
        elif token == "--ack-all" and not ack_all and ack_id is None:
            ack_all = True
            index += 1
        elif (
            token == "--client"
            and client_override is None
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
            and args[index + 1]
        ):
            client_override = args[index + 1]
            index += 2
        elif (
            token == "--session"
            and session_override is None
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
            and args[index + 1]
        ):
            session_override = args[index + 1]
            index += 2
        else:
            return None
    if (client_override is None) != (session_override is None):
        return None
    if client_override is not None and client_override not in ("codex", "claude"):
        return None
    return ack_id, ack_all, client_override, session_override


def _parse_watch_args(
    args: Sequence[str],
) -> tuple[list[str], str | None, int, float, str | None, str | None] | None:
    paths: list[str] = []
    target_session: str | None = None
    timeout_seconds = coordination.DEFAULT_WATCH_TIMEOUT_SECONDS
    interval_seconds = coordination.DEFAULT_WATCH_INTERVAL_SECONDS
    client_override: str | None = None
    session_self_override: str | None = None
    timeout_seen = False
    interval_seen = False
    index = 0
    while index < len(args):
        token = args[index]
        has_value = (
            index + 1 < len(args)
            and not args[index + 1].startswith("--")
            and bool(args[index + 1])
        )
        if token == "--paths" and has_value:
            paths.append(args[index + 1])
            index += 2
        elif token == "--session" and target_session is None and has_value:
            target_session = args[index + 1]
            index += 2
        elif token == "--timeout-seconds" and not timeout_seen and has_value:
            try:
                timeout_seconds = int(args[index + 1])
            except ValueError:
                return None
            timeout_seen = True
            index += 2
        elif token == "--interval-seconds" and not interval_seen and has_value:
            try:
                interval_seconds = float(args[index + 1])
            except ValueError:
                return None
            interval_seen = True
            index += 2
        elif token == "--client" and client_override is None and has_value:
            client_override = args[index + 1]
            index += 2
        elif (
            token == "--session-self"
            and session_self_override is None
            and has_value
        ):
            session_self_override = args[index + 1]
            index += 2
        else:
            return None
    if (client_override is None) != (session_self_override is None):
        return None
    if client_override is not None and client_override not in ("codex", "claude"):
        return None
    if not math.isfinite(interval_seconds):
        return None
    return (
        paths,
        target_session,
        timeout_seconds,
        interval_seconds,
        client_override,
        session_self_override,
    )


def _usage(stderr: TextIO | None = None) -> int:
    print(
        "usage: agent_session_status.py hook | status [--json] | identity | "
        "presence | "
        "claim [--client <client> --session <id>] [--paths <pathspec>]... "
        "<label> | claim --done [--client <client> --session <id>] | "
        "note <text> | note --done <id> | "
        "conflicts [--paths <pathspec>]... | "
        "msg [--client <codex|claude> --session <id>] <target> <text> | "
        "inbox [--client <codex|claude> --session <id>] "
        "[--ack <id> | --ack-all] | "
        "watch [--paths <pathspec>]... [--session <id-or-prefix>] "
        "[--timeout-seconds <n>] [--interval-seconds <s>] "
        "[--client <codex|claude> --session-self <id>]",
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
        label, done, client_override, session_override, paths = parsed
        return run_claim(
            label,
            client_override=client_override,
            session_override=session_override,
            paths=paths,
            done=done,
        )
    if verb == "note":
        parsed = _parse_note_args(rest)
        if parsed is None:
            return _usage()
        text, done_id = parsed
        return run_note(text=text, done_id=done_id)
    if verb == "conflicts":
        parsed = _parse_conflicts_args(rest)
        if parsed is None:
            return _usage()
        return run_conflicts(paths=parsed)
    if verb == "msg":
        parsed = _parse_msg_args(rest)
        if parsed is None:
            return _usage()
        target, text, client_override, session_override = parsed
        return run_msg(
            target,
            text,
            client_override=client_override,
            session_override=session_override,
        )
    if verb == "inbox":
        parsed = _parse_inbox_args(rest)
        if parsed is None:
            return _usage()
        ack_id, ack_all, client_override, session_override = parsed
        return run_inbox(
            ack_id=ack_id,
            ack_all=ack_all,
            client_override=client_override,
            session_override=session_override,
        )
    if verb == "watch":
        parsed = _parse_watch_args(rest)
        if parsed is None:
            return _usage()
        (
            paths,
            target_session,
            timeout_seconds,
            interval_seconds,
            client_override,
            session_self_override,
        ) = parsed
        return run_watch(
            paths=paths,
            target_session=target_session,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            client_override=client_override,
            session_self_override=session_self_override,
        )

    return _usage()
