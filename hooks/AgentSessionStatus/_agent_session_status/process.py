"""Operating-system process inspection for session liveness and identity."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

PS_TIMEOUT_SECONDS = 1.0
CODEX_IDENTITY_TIMEOUT_SECONDS = 1.5
ProcessIdentity = tuple[int, str]
FingerprintLookup = Callable[[int], str | None]


def ps_value(pid: int, field: str, timeout_seconds: float = PS_TIMEOUT_SECONDS) -> str:
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
    started = ps_value(pid, "lstart", timeout_seconds)
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

    def read_ps_value(field: str) -> str:
        remaining = deadline - time.monotonic()
        return ps_value(pid, field, min(PS_TIMEOUT_SECONDS, remaining))

    for _ in range(16):
        if pid <= 1 or pid in visited or time.monotonic() >= deadline:
            break
        visited.add(pid)

        command_name = read_ps_value("comm")
        command = read_ps_value("command")
        if _looks_like_codex_process(command_name, command):
            fingerprint = process_start_fingerprint(
                pid, min(PS_TIMEOUT_SECONDS, deadline - time.monotonic())
            )
            if fingerprint:
                return pid, fingerprint

        parent = read_ps_value("ppid")
        try:
            pid = int(parent)
        except ValueError:
            break

    return None


def process_ancestors(
    start_pid: int,
    timeout_seconds: float = CODEX_IDENTITY_TIMEOUT_SECONDS,
    ps_value: Callable[[int, str, float], str] = ps_value,
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
