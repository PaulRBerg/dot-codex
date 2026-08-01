"""Command-line commands, argument parsing, and dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from . import codex, inventory, registry
from ._core import codex_home as default_codex_home
from ._core import registry_dir, warn
from .process import FingerprintLookup, process_start_fingerprint


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
