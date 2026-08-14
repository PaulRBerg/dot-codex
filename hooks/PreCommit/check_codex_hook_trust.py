#!/usr/bin/env python3
"""Reject trusted Codex hook keys that the staged configuration no longer discovers."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any


APP_SERVER_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1024 * 1024


class TrustCheckError(RuntimeError):
    """A failure that prevents proving the staged trust state is current."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-home", type=Path, required=True)
    parser.add_argument("--canonical-home", type=Path, required=True)
    return parser.parse_args()


def split_hook_key(key: str) -> tuple[Path, str]:
    parts = key.rsplit(":", 3)
    if len(parts) != 4 or not parts[0] or not parts[1] or not parts[2].isdigit() or not parts[3].isdigit():
        raise TrustCheckError(f"malformed Codex hook trust key: {key}")
    return Path(parts[0]), f":{parts[1]}:{parts[2]}:{parts[3]}"


def trusted_hook_keys(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []
    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise TrustCheckError(f"could not read staged Codex config: {error}") from error

    hooks = config.get("hooks", {})
    state = hooks.get("state", {}) if isinstance(hooks, dict) else None
    if not isinstance(state, dict):
        raise TrustCheckError("staged Codex config has malformed hooks.state")

    provisions = []
    for key, provision in state.items():
        if not isinstance(key, str) or not isinstance(provision, dict):
            raise TrustCheckError("staged Codex config has malformed hook state")
        if "trusted_hash" in provision:
            provisions.append(key)
    return provisions


def normalized_state_key(key: str) -> str:
    source_path, suffix = split_hook_key(key)
    return f"{source_path.expanduser().resolve(strict=False)}{suffix}"


def discovery_cwds(keys: list[str], canonical_home: Path) -> list[str]:
    cwds = {canonical_home}
    for key in keys:
        source_path, _suffix = split_hook_key(key)
        source_path = source_path.expanduser().resolve(strict=False)
        if source_path.is_relative_to(canonical_home):
            continue
        codex_indexes = [index for index, part in enumerate(source_path.parts) if part == ".codex"]
        if not codex_indexes:
            continue
        project_root = Path(*source_path.parts[: codex_indexes[-1]])
        if project_root.is_dir():
            cwds.add(project_root.resolve())
    return sorted(str(cwd) for cwd in cwds)


def send_message(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(f"{json.dumps(message, separators=(',', ':'))}\n")
    process.stdin.flush()


def receive_response(
    process: subprocess.Popen[str], selector: selectors.BaseSelector, request_id: int, deadline: float
) -> dict[str, Any]:
    assert process.stdout is not None
    received_bytes = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not selector.select(remaining):
            raise TrustCheckError("Codex app-server timed out")
        line = process.stdout.readline()
        if not line:
            raise TrustCheckError("Codex app-server exited before responding")
        received_bytes += len(line.encode())
        if received_bytes > MAX_RESPONSE_BYTES:
            raise TrustCheckError("Codex app-server response exceeded the size limit")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise TrustCheckError("Codex app-server returned malformed JSON") from error
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message:
            raise TrustCheckError(f"Codex app-server request failed: {message['error']}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise TrustCheckError("Codex app-server returned a malformed response")
        return result


def stderr_excerpt(stderr_file: Any) -> str:
    stderr_file.seek(0)
    return stderr_file.read(4096).decode(errors="replace").strip()


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def app_server_hooks(snapshot_home: Path, cwds: list[str]) -> list[dict[str, Any]]:
    environment = os.environ | {"CODEX_HOME": str(snapshot_home)}
    with tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                ["codex", "app-server"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise TrustCheckError(f"could not start Codex app-server: {error}") from error

        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + APP_SERVER_TIMEOUT_SECONDS
        try:
            send_message(
                process,
                {
                    "method": "initialize",
                    "id": 0,
                    "params": {
                        "clientInfo": {
                            "name": "dot_codex_precommit",
                            "title": "Dot Codex Pre-commit",
                            "version": "1.0.0",
                        }
                    },
                },
            )
            receive_response(process, selector, 0, deadline)
            send_message(process, {"method": "initialized", "params": {}})
            send_message(process, {"method": "hooks/list", "id": 1, "params": {"cwds": cwds}})
            response = receive_response(process, selector, 1, deadline)
            assert process.stdin is not None
            process.stdin.close()
            try:
                return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise TrustCheckError("Codex app-server did not exit after responding") from error
            if return_code != 0:
                detail = stderr_excerpt(stderr_file)
                suffix = f": {detail}" if detail else ""
                raise TrustCheckError(f"Codex app-server exited with status {return_code}{suffix}")
        finally:
            selector.close()
            stop_process(process)

    data = response.get("data")
    if not isinstance(data, list):
        raise TrustCheckError("Codex hooks/list returned malformed data")
    hooks: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            raise TrustCheckError("Codex hooks/list returned a malformed entry")
        errors = entry.get("errors")
        if not isinstance(errors, list):
            raise TrustCheckError("Codex hooks/list returned malformed errors")
        if errors:
            raise TrustCheckError(f"Codex reported hook discovery errors: {json.dumps(errors, separators=(',', ':'))}")
        hooks.extend(entry["hooks"])
    return hooks


def normalized_discovered_key(hook: dict[str, Any], snapshot_home: Path, canonical_home: Path) -> str:
    key = hook.get("key")
    source = hook.get("sourcePath")
    if not isinstance(key, str) or not isinstance(source, str) or not key.startswith(f"{source}:"):
        raise TrustCheckError("Codex hooks/list returned malformed hook metadata")
    _key_source, suffix = split_hook_key(key)
    source_path = Path(source).resolve(strict=False)
    if source_path.is_relative_to(snapshot_home):
        source_path = canonical_home / source_path.relative_to(snapshot_home)
    return f"{source_path.resolve(strict=False)}{suffix}"


def stale_provisions(snapshot_home: Path, canonical_home: Path) -> list[str]:
    provisions = trusted_hook_keys(snapshot_home / "config.toml")
    if not provisions:
        return []
    cwds = discovery_cwds(provisions, canonical_home)
    discovered = {
        normalized_discovered_key(hook, snapshot_home, canonical_home)
        for hook in app_server_hooks(snapshot_home, cwds)
    }
    return sorted(key for key in provisions if normalized_state_key(key) not in discovered)


def main() -> int:
    arguments = parse_arguments()
    snapshot_home = arguments.snapshot_home.resolve()
    canonical_home = arguments.canonical_home.resolve()
    try:
        stale = stale_provisions(snapshot_home, canonical_home)
    except TrustCheckError as error:
        print(f"error: could not validate staged Codex hook trust state: {error}", file=sys.stderr)
        return 1
    if not stale:
        return 0

    print("error: stale Codex hook trust provisions must be removed:", file=sys.stderr)
    for key in stale:
        print(f"  [hooks.state.{json.dumps(key)}]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
