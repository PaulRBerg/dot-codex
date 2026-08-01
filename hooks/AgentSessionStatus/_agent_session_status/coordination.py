"""Cross-session conflicts, messaging, inbox, and watch commands."""

from __future__ import annotations

import fnmatch
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from . import inventory, registry
from ._core import codex_home as default_codex_home
from ._core import format_age, registry_dir, utc_now, warn

GIT_TIMEOUT_SECONDS = 5.0
DEFAULT_WATCH_INTERVAL_SECONDS = 3.0
DEFAULT_WATCH_TIMEOUT_SECONDS = 300
MIN_WATCH_INTERVAL_SECONDS = 1.0
MAX_WATCH_INTERVAL_SECONDS = 30.0
MAX_WATCH_TIMEOUT_SECONDS = 3600.0

InventoryBuilder = Callable[[], dict[str, Any]]
GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def _sanitize(value: object, limit: int = registry.MAX_MESSAGE_CHARS) -> str:
    # cli owns the shared cross-session sanitizer. Import lazily so cli can expose
    # these command functions without creating an import cycle.
    from .cli import _sanitize_text

    return _sanitize_text(str(value), limit)


def _write_tsv(stdout: TextIO, *cells: object) -> None:
    stdout.write("\t".join(_sanitize(cell) for cell in cells) + "\n")


def _path_within(path: str, root: str) -> bool:
    from .cli import _path_within as cli_path_within

    return cli_path_within(Path(path), Path(root))


def _inventory(
    builder: InventoryBuilder | None, codex_home: Path
) -> dict[str, Any]:
    return (
        builder()
        if builder is not None
        else inventory.build_inventory(codex_home=codex_home, stderr=StringIO())
    )


def _sessions(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = document.get("sessions")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _resolve_identity(
    codex_home: Path,
    client_override: str | None,
    session_override: str | None,
    identity: dict[str, str] | None,
) -> dict[str, str] | None:
    if client_override is not None and session_override is not None:
        return {"client": client_override, "session_id": session_override}
    if client_override is not None or session_override is not None:
        return None
    return (
        identity
        if identity is not None
        else inventory.resolve_identity(codex_home=codex_home)
    )


def _git_root(cwd: str, runner: GitRunner) -> str | None:
    try:
        result = runner(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.removesuffix("\n").removesuffix("\r")
    return root or None


def cover(spec: str, path: str) -> bool:
    """Return whether spec conservatively covers path."""
    if spec == ".":
        # normalize_spec maps the repo root itself to "."; it covers everything.
        return True
    return fnmatch.fnmatchcase(path, spec) or fnmatch.fnmatchcase(
        path, spec + "/*"
    )


def specs_intersect(left: str, right: str) -> bool:
    """Return whether either pathspec conservatively covers the other."""
    return cover(left, right) or cover(right, left)


def _same_repo_claim(claim: dict[str, Any], root: str) -> bool:
    claim_root = claim.get("repo_root")
    if isinstance(claim_root, str):
        return claim_root == root
    # Schema-v1 claims predate repo_root. They remain visible as unscoped
    # information when their recorded cwd is within this repository.
    cwd = claim.get("cwd")
    return isinstance(cwd, str) and _path_within(cwd, root)


def _dirty_paths(output: str) -> list[str]:
    fields = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        status = field[:2]
        path = field[3:] if len(field) >= 3 else ""
        if path:
            paths.append(path)
        if "R" in status or "C" in status:
            index += 1
    return paths


def run_conflicts(
    paths: Sequence[str] = (),
    *,
    codex_home: Path | None = None,
    cwd: str | None = None,
    identity: dict[str, str] | None = None,
    inventory_builder: InventoryBuilder | None = None,
    git_runner: GitRunner = subprocess.run,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Report live claim overlaps and ownership of relevant dirty files."""
    home = codex_home or default_codex_home()
    current_cwd = cwd or os.getcwd()
    root = registry.repo_root(current_cwd, runner=git_runner)
    resolved = (
        identity
        if identity is not None
        else inventory.resolve_identity(codex_home=home)
    )

    try:
        document = _inventory(inventory_builder, home)
        claims = registry.load_claims(registry_dir(home))
    except (OSError, RuntimeError) as error:
        warn(f"could not read coordination registry: {error}", stderr)
        return 1

    query_specs: list[str] = []
    if paths:
        for spec in paths:
            normalized = registry.normalize_spec(spec, current_cwd, root)
            if normalized is None:
                warn(f"pathspec {_sanitize(spec)!r} is outside repo root {root}", stderr)
                return os.EX_USAGE
            query_specs.append(normalized)
    else:
        own_claim = None
        if resolved is not None:
            own_claim = claims.get((resolved["client"], resolved["session_id"]))
        if own_claim is None or not own_claim.get("paths"):
            warn("no claimed paths; pass --paths <pathspec> or set claim paths", stderr)
            return os.EX_USAGE
        query_specs = list(own_claim["paths"])

    live_keys = {
        (row.get("client"), row.get("session_id")) for row in _sessions(document)
    }
    own_key = (
        (resolved["client"], resolved["session_id"])
        if resolved is not None
        else None
    )
    scoped_claims: list[dict[str, Any]] = []
    unscoped_claims: list[dict[str, Any]] = []
    overlaps: list[tuple[dict[str, Any], list[str]]] = []
    for key, claim in claims.items():
        if key not in live_keys or key == own_key or not _same_repo_claim(claim, root):
            continue
        their_specs = claim.get("paths")
        if not isinstance(their_specs, list) or not their_specs:
            unscoped_claims.append(claim)
            continue
        scoped_claims.append(claim)
        matching = [
            their
            for their in their_specs
            if any(specs_intersect(query, their) for query in query_specs)
        ]
        if matching:
            overlaps.append((claim, matching))

    _write_tsv(stdout, "TYPE", "CLIENT/OWNER", "SESSION/PATH", "LABEL", "SPECS")
    for claim, matching in overlaps:
        _write_tsv(
            stdout,
            "OVERLAP",
            claim["client"],
            claim["session_id"][:8],
            claim["label"],
            ",".join(matching),
        )
    for claim in unscoped_claims:
        _write_tsv(
            stdout,
            "UNSCOPED",
            claim["client"],
            claim["session_id"][:8],
            claim["label"],
            "",
        )

    try:
        dirty_result = git_runner(
            ["git", "-C", root, "status", "--porcelain=v1", "-z"],
            capture_output=True,
            check=False,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        dirty_result = None
    if dirty_result is None or dirty_result.returncode != 0:
        stdout.write("dirty: unavailable\n")
    else:
        for dirty_path in _dirty_paths(dirty_result.stdout):
            if not any(cover(query, dirty_path) for query in query_specs):
                continue
            owners = {
                (claim["client"], claim["session_id"])
                for claim in scoped_claims
                if any(cover(their, dirty_path) for their in claim["paths"])
            }
            if len(owners) > 1:
                owner = "contested"
            elif len(owners) == 1:
                owner_client, owner_session = next(iter(owners))
                owner = f"{owner_client}/{owner_session[:8]}"
            else:
                owner = "unknown"
            _write_tsv(stdout, "DIRTY", owner, dirty_path)

    return 1 if overlaps else 0


def _candidate_name(session: dict[str, Any]) -> str:
    label = session.get("label")
    if isinstance(label, str) and label:
        return label
    name = session.get("name")
    if session.get("client") == "claude" and isinstance(name, str) and name:
        return name
    return ""


def _candidate_line(session: dict[str, Any]) -> str:
    client = _sanitize(session.get("client", ""))
    session_id = _sanitize(session.get("session_id", ""))
    name = _sanitize(_candidate_name(session), 80)
    return f"{client}/{session_id[:8]}  {name}"


def resolve_msg_targets(
    target: str,
    sessions: Sequence[dict[str, Any]],
    sender: dict[str, str],
    root: str,
) -> tuple[list[dict[str, Any]], str | None, list[dict[str, Any]]]:
    """Resolve a msg target using the specified first-match ladder."""
    live = [
        row
        for row in sessions
        if isinstance(row.get("client"), str)
        and isinstance(row.get("session_id"), str)
    ]
    if target == "repo":
        recipients = [
            row
            for row in live
            if isinstance(row.get("cwd"), str)
            and _path_within(row["cwd"], root)
            and not (
                row["client"] == sender["client"]
                and row["session_id"] == sender["session_id"]
            )
        ]
        return recipients, None if recipients else "no live sessions in this repo", []

    exact = [row for row in live if row["session_id"] == target]
    if exact:
        return (
            exact if len(exact) == 1 else [],
            None if len(exact) == 1 else "ambiguous target",
            exact if len(exact) > 1 else [],
        )

    prefix = [row for row in live if row["session_id"].startswith(target)]
    if prefix and len(target) < 4:
        return [], "session prefix must be at least 4 characters", []
    if prefix:
        return (
            prefix if len(prefix) == 1 else [],
            None if len(prefix) == 1 else "ambiguous target",
            prefix if len(prefix) > 1 else [],
        )

    lowered = target.casefold()
    substring = [
        row for row in live if lowered in _candidate_name(row).casefold()
    ]
    if substring:
        return (
            substring if len(substring) == 1 else [],
            None if len(substring) == 1 else "ambiguous target",
            substring if len(substring) > 1 else [],
        )
    return [], "no matching session", []


def run_msg(
    target: str,
    text: str,
    *,
    client_override: str | None = None,
    session_override: str | None = None,
    codex_home: Path | None = None,
    cwd: str | None = None,
    identity: dict[str, str] | None = None,
    inventory_builder: InventoryBuilder | None = None,
    git_runner: GitRunner = subprocess.run,
    now: str | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    home = codex_home or default_codex_home()
    if (client_override is None) != (session_override is None):
        return os.EX_USAGE
    if client_override is not None and client_override not in ("codex", "claude"):
        return os.EX_USAGE
    sender = _resolve_identity(
        home, client_override, session_override, identity
    )
    if sender is None:
        warn(
            "could not resolve sender; pass --client <codex|claude> --session <id>",
            stderr,
        )
        return 1

    sanitized_text = _sanitize(text, registry.MAX_MESSAGE_CHARS)
    if not sanitized_text:
        warn("message text is empty after sanitization", stderr)
        return os.EX_USAGE
    current_cwd = cwd or os.getcwd()
    root = registry.repo_root(current_cwd, runner=git_runner)
    storage = registry_dir(home)
    try:
        document = _inventory(inventory_builder, home)
        claims = registry.load_claims(storage)
    except (OSError, RuntimeError) as error:
        warn(f"could not read live sessions: {error}", stderr)
        return 1

    live = []
    for row in _sessions(document):
        enriched = dict(row)
        claim = claims.get((row.get("client"), row.get("session_id")))
        if claim is not None:
            enriched["label"] = claim["label"]
        live.append(enriched)
    recipients, error, candidates = resolve_msg_targets(target, live, sender, root)
    if error is not None:
        warn(error, stderr)
        for candidate in candidates:
            print(_candidate_line(candidate), file=stderr)
        return 1

    sender_claim = claims.get((sender["client"], sender["session_id"]))
    from_label = sender_claim["label"] if sender_claim is not None else None
    entries: list[dict[str, Any]] = []
    try:
        for recipient in recipients:
            entries.append(
                registry.add_message(
                    storage,
                    recipient["client"],
                    recipient["session_id"],
                    {
                        "from_client": sender["client"],
                        "from_session_id": sender["session_id"],
                        "from_label": _sanitize(from_label, 80)
                        if from_label
                        else None,
                        "text": sanitized_text,
                        "repo_root": root,
                    },
                    now=now,
                )
            )
    except (OSError, RuntimeError, ValueError) as error:
        warn(f"could not send message: {error}", stderr)
        return 1
    stdout.write(f"sent {_sanitize(entries[0]['id'])} to {len(entries)} session(s)\n")
    return 0


def run_inbox(
    *,
    ack_id: str | None = None,
    ack_all: bool = False,
    client_override: str | None = None,
    session_override: str | None = None,
    codex_home: Path | None = None,
    identity: dict[str, str] | None = None,
    now: str | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    if ack_id is not None and ack_all:
        return os.EX_USAGE
    if (client_override is None) != (session_override is None):
        return os.EX_USAGE
    home = codex_home or default_codex_home()
    if client_override is not None and client_override not in ("codex", "claude"):
        return os.EX_USAGE
    resolved = _resolve_identity(
        home, client_override, session_override, identity
    )
    if resolved is None:
        warn(
            "could not resolve inbox; pass --client <codex|claude> --session <id>",
            stderr,
        )
        return 1
    storage = registry_dir(home)
    try:
        entries = registry.load_inbox(
            storage, resolved["client"], resolved["session_id"], now=now
        )
    except OSError as error:
        warn(f"could not read inbox: {error}", stderr)
        return 1

    if ack_id is not None:
        if not any(entry["id"] == ack_id for entry in entries):
            warn(f"unknown message id {_sanitize(ack_id)!r}", stderr)
            return 1
        removed = registry.ack_messages(
            storage, resolved["client"], resolved["session_id"], [ack_id]
        )
        stdout.write(f"acked {removed}\n")
        return 0
    if ack_all:
        removed = registry.ack_messages(
            storage, resolved["client"], resolved["session_id"], None
        )
        stdout.write(f"acked {removed}\n")
        return 0

    if not entries:
        stdout.write("No messages.\n")
        return 0
    _write_tsv(stdout, "ID", "AGE", "FROM", "TEXT")
    for entry in entries:
        sender = f"{entry['from_client']}/{entry['from_session_id'][:8]}"
        label = entry.get("from_label")
        if isinstance(label, str) and label:
            sender += f"  {_sanitize(label, 80)}"
        _write_tsv(
            stdout,
            entry["id"],
            format_age(entry.get("age_seconds")),
            sender,
            entry["text"],
        )
    return 0


def _watch_session_candidates(
    target: str, sessions: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    exact = [row for row in sessions if row.get("session_id") == target]
    if exact:
        return exact
    return [
        row
        for row in sessions
        if isinstance(row.get("session_id"), str)
        and row["session_id"].startswith(target)
    ]


def _clock_value(clock: Callable[[], str] | str | None) -> str:
    if callable(clock):
        return clock()
    return clock if isinstance(clock, str) else utc_now()


def _format_seconds(value: float) -> str:
    return f"{value:g}"


def _wake(stdout: TextIO, reason: str, detail: str) -> int:
    _write_tsv(stdout, reason, detail)
    return 0


def run_watch(
    paths: Sequence[str] = (),
    *,
    target_session: str | None = None,
    timeout_seconds: int = DEFAULT_WATCH_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS,
    client_override: str | None = None,
    session_self_override: str | None = None,
    codex_home: Path | None = None,
    cwd: str | None = None,
    identity: dict[str, str] | None = None,
    now: Callable[[], str] | str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    inventory_builder: InventoryBuilder | None = None,
    git_runner: GitRunner = subprocess.run,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    if (client_override is None) != (session_self_override is None):
        return os.EX_USAGE
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not isinstance(interval_seconds, (int, float))
        or isinstance(interval_seconds, bool)
        or not math.isfinite(float(interval_seconds))
    ):
        return os.EX_USAGE
    interval = min(
        max(float(interval_seconds), MIN_WATCH_INTERVAL_SECONDS),
        MAX_WATCH_INTERVAL_SECONDS,
    )
    timeout = min(
        max(float(timeout_seconds), interval), MAX_WATCH_TIMEOUT_SECONDS
    )
    if client_override is not None and client_override not in ("codex", "claude"):
        return os.EX_USAGE
    home = codex_home or default_codex_home()
    storage = registry_dir(home)
    current_cwd = cwd or os.getcwd()
    discovered_root = _git_root(current_cwd, git_runner)
    if paths and discovered_root is None:
        warn("--paths requires being inside a git repository", stderr)
        return os.EX_USAGE
    root = discovered_root or current_cwd

    resolved = _resolve_identity(
        home, client_override, session_self_override, identity
    )
    if resolved is None:
        warn(
            "could not resolve watch identity; message condition disabled; "
            "pass --client <codex|claude> --session-self <id>",
            stderr,
        )

    try:
        baseline_notes = {
            entry["id"]
            for entry in registry.load_notes(
                storage, now=_clock_value(now), stderr=stderr
            ).get(root, [])
        }
    except (OSError, RuntimeError) as error:
        warn(f"could not arm note watch: {error}", stderr)
        return 1

    watched_session: dict[str, Any] | None = None
    if target_session is not None:
        try:
            arm_document = _inventory(inventory_builder, home)
        except (OSError, RuntimeError) as error:
            warn(f"could not arm session watch: {error}", stderr)
            return 1
        candidates = _watch_session_candidates(
            target_session, _sessions(arm_document)
        )
        if not candidates:
            return _wake(stdout, "session-gone", target_session)
        if len(candidates) > 1:
            warn("ambiguous session target", stderr)
            for candidate in candidates:
                print(_candidate_line(candidate), file=stderr)
            return os.EX_USAGE
        watched_session = candidates[0]

    start = monotonic()
    deadline = start + timeout
    while True:
        timestamp = _clock_value(now)
        try:
            if resolved is not None:
                pending = registry.count_pending(
                    storage, resolved["session_id"], now=timestamp
                )
                if pending:
                    return _wake(stdout, "message", f"{pending} pending")

            note_ids = {
                entry["id"]
                for entry in registry.load_notes(
                    storage, now=timestamp, stderr=stderr
                ).get(root, [])
            }
        except (OSError, RuntimeError) as error:
            warn(f"could not poll coordination registry: {error}", stderr)
            return 1
        new_note_ids = note_ids - baseline_notes
        if new_note_ids:
            return _wake(stdout, "note", f"{len(new_note_ids)} new")

        if paths:
            try:
                result = git_runner(
                    [
                        "git",
                        "-C",
                        current_cwd,
                        "status",
                        "--porcelain",
                        "--",
                        *paths,
                    ],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=GIT_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError) as error:
                warn(f"could not poll paths: {error}", stderr)
                return 1
            if result.returncode != 0:
                warn(
                    f"could not poll paths: {result.stderr.strip() or f'exit {result.returncode}'}",
                    stderr,
                )
                return 1
            if not result.stdout:
                return _wake(stdout, "paths-clean", ",".join(paths))

        if watched_session is not None:
            try:
                document = _inventory(inventory_builder, home)
            except (OSError, RuntimeError) as error:
                warn(f"could not poll live sessions: {error}", stderr)
                return 1
            full_id = watched_session["session_id"]
            present = any(
                row.get("session_id") == full_id for row in _sessions(document)
            )
            provider = document.get("providers", {}).get(
                watched_session.get("client"), {}
            )
            if not present and isinstance(provider, dict) and provider.get("ok"):
                return _wake(stdout, "session-gone", full_id)

        current = monotonic()
        if current >= deadline:
            _write_tsv(stdout, "timeout", f"waited {_format_seconds(timeout)}s")
            return 3
        sleep(min(interval, deadline - current))
