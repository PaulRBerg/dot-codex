#!/usr/bin/env -S uv run python
"""Unit tests for the cross-client agent-session status hook."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import agent_session_status as status  # noqa: E402, RUF100


def _prompt_event(session_id: str = "session-1", turn_id: str = "turn-1") -> dict:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": "/tmp/project",
        "prompt": "private prompt text",
        "transcript_path": "/tmp/private-transcript.jsonl",
        "last_assistant_message": "private assistant text",
        "tool_data": {"secret": "private tool data"},
    }


def _stop_event(event: str = "Stop", session_id: str = "session-1") -> dict:
    return {
        "hook_event_name": event,
        "session_id": session_id,
        "turn_id": "turn-1",
        "cwd": "/tmp/project",
    }


def _raise_lookup_error(_pid: int) -> str | None:
    raise RuntimeError("lookup failed")


def _write_hooks(home: Path, script_path: Path) -> None:
    handlers = {
        event: [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{script_path} hook",
                    }
                ]
            }
        ]
        for event in status.HOOK_EVENTS
    }
    (home / "hooks.json").write_text(json.dumps({"hooks": handlers}), encoding="utf-8")


def _complete_document() -> dict:
    return {
        "schema_version": 1,
        "complete": True,
        "providers": {
            "codex": {"ok": True, "source": status.CODEX_SOURCE},
            "claude": {"ok": True, "source": status.CLAUDE_SOURCE},
        },
        "sessions": [
            {
                "client": "codex",
                "session_id": "session-1",
                "state": "in_flight",
                "cwd": "/tmp/project",
                "pid": 123,
                "started_at": "2026-08-01T10:00:00.000Z",
            }
        ],
    }


class TestProcessIdentity(unittest.TestCase):
    def test_ancestor_lookup_respects_total_timeout(self) -> None:
        clock = [0.0]
        calls = []

        def monotonic() -> float:
            clock[0] += 0.2
            return clock[0]

        def ps_value(pid: int, field: str, timeout_seconds: float) -> str:
            calls.append((pid, field, timeout_seconds))
            if field == "ppid":
                return str(pid - 1)
            return "python"

        with (
            patch.object(status.os, "getppid", return_value=100),
            patch.object(status.time, "monotonic", side_effect=monotonic),
            patch.object(status, "_ps_value", side_effect=ps_value),
        ):
            identity = status.codex_process_identity(timeout_seconds=1.0)

        self.assertIsNone(identity)
        self.assertEqual(
            [field for _pid, field, _timeout in calls],
            ["comm", "command", "ppid"],
        )
        self.assertTrue(all(0 < timeout <= 1.0 for _pid, _field, timeout in calls))


class TestHookTransitions(unittest.TestCase):
    def test_prompt_writes_and_stop_removes_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry"
            status.handle_hook_event(
                _prompt_event(),
                registry,
                process_identity=(123, "fingerprint"),
                now="2026-08-01T10:00:00.000Z",
            )

            records = list(registry.glob("*.json"))
            self.assertEqual(len(records), 1)

            status.handle_hook_event(
                _stop_event(),
                registry,
                fingerprint_lookup=lambda _pid: "fingerprint",
            )
            self.assertEqual(list(registry.glob("*.json")), [])

    def test_session_end_removes_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry"
            status.handle_hook_event(
                _prompt_event(),
                registry,
                process_identity=(123, "fingerprint"),
            )
            status.handle_hook_event(
                _stop_event("SessionEnd"),
                registry,
                fingerprint_lookup=lambda _pid: "fingerprint",
            )

            self.assertEqual(list(registry.glob("*.json")), [])

    def test_duplicate_prompt_preserves_started_at(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry"
            status.handle_hook_event(
                _prompt_event(),
                registry,
                process_identity=(123, "fingerprint"),
                now="2026-08-01T10:00:00.000Z",
            )
            status.handle_hook_event(
                _prompt_event(),
                registry,
                process_identity=(123, "fingerprint"),
                fingerprint_lookup=lambda _pid: "fingerprint",
                now="2026-08-01T10:01:00.000Z",
            )

            record = json.loads(next(registry.glob("*.json")).read_text())
            self.assertEqual(record["started_at"], "2026-08-01T10:00:00.000Z")
            self.assertEqual(record["updated_at"], "2026-08-01T10:01:00.000Z")

    def test_records_are_atomic_private_and_concurrent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry"

            def write(index: int) -> None:
                status.handle_hook_event(
                    _prompt_event(f"session-{index}", f"turn-{index}"),
                    registry,
                    process_identity=(1000 + index, f"fingerprint-{index}"),
                    fingerprint_lookup=lambda pid: f"fingerprint-{pid - 1000}",
                    now=f"2026-08-01T10:00:{index:02d}.000Z",
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(20)))

            records = list(registry.glob("*.json"))
            self.assertEqual(len(records), 20)
            self.assertEqual(stat.S_IMODE(registry.stat().st_mode), 0o700)
            for path in records:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(json.loads(path.read_text())["schema_version"], 1)
            self.assertEqual(list(registry.glob(".*.tmp-*")), [])

    def test_concurrent_replacement_never_leaves_partial_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry"

            def write(index: int) -> None:
                status.handle_hook_event(
                    _prompt_event("shared-session", "shared-turn"),
                    registry,
                    process_identity=(123, "fingerprint"),
                    fingerprint_lookup=lambda _pid: "fingerprint",
                    now=f"2026-08-01T10:00:{index:02d}.000Z",
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(20)))

            records = list(registry.glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(
                json.loads(records[0].read_text())["session_id"], "shared-session"
            )

    def test_record_excludes_private_hook_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry"
            status.handle_hook_event(
                _prompt_event(),
                registry,
                process_identity=(123, "fingerprint"),
            )

            raw = next(registry.glob("*.json")).read_text()
            record = json.loads(raw)
            self.assertEqual(set(record), status.RECORD_FIELDS)
            self.assertNotIn("private prompt text", raw)
            self.assertNotIn("private-transcript", raw)
            self.assertNotIn("private assistant text", raw)
            self.assertNotIn("private tool data", raw)

    def test_prompt_is_recorded_before_pruning_other_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry"
            status.handle_hook_event(
                _prompt_event("old-session"),
                registry,
                process_identity=(123, "fingerprint"),
            )

            with self.assertRaisesRegex(RuntimeError, "lookup failed"):
                status.handle_hook_event(
                    _prompt_event("new-session"),
                    registry,
                    process_identity=(124, "new-fingerprint"),
                    fingerprint_lookup=_raise_lookup_error,
                )

            session_ids = {
                json.loads(path.read_text())["session_id"]
                for path in registry.glob("*.json")
            }
            self.assertIn("new-session", session_ids)

    def test_stop_removes_record_before_pruning_other_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry"
            for session_id in ("stopped-session", "other-session"):
                status.handle_hook_event(
                    _prompt_event(session_id),
                    registry,
                    process_identity=(123, "fingerprint"),
                    fingerprint_lookup=lambda _pid: "fingerprint",
                )

            with self.assertRaisesRegex(RuntimeError, "lookup failed"):
                status.handle_hook_event(
                    _stop_event(session_id="stopped-session"),
                    registry,
                    fingerprint_lookup=_raise_lookup_error,
                )

            session_ids = {
                json.loads(path.read_text())["session_id"]
                for path in registry.glob("*.json")
            }
            self.assertNotIn("stopped-session", session_ids)


class TestRegistryLiveness(unittest.TestCase):
    def _create_home(self, root: Path, fingerprint: str = "fingerprint") -> Path:
        home = root / "codex-home"
        home.mkdir()
        _write_hooks(home, Path(status.__file__).resolve())
        registry = home / status.REGISTRY_RELATIVE_PATH
        status.handle_hook_event(
            _prompt_event(),
            registry,
            process_identity=(123, fingerprint),
        )
        return home

    def test_matching_process_fingerprint_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary))

            provider, sessions = status.collect_codex_sessions(
                home, fingerprint_lookup=lambda _pid: "fingerprint"
            )

            self.assertTrue(provider["ok"])
            self.assertEqual([row["session_id"] for row in sessions], ["session-1"])

    def test_dead_process_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary))

            provider, sessions = status.collect_codex_sessions(
                home, fingerprint_lookup=lambda _pid: None
            )

            self.assertTrue(provider["ok"])
            self.assertEqual(sessions, [])

    def test_reused_pid_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary), fingerprint="old-start")

            provider, sessions = status.collect_codex_sessions(
                home, fingerprint_lookup=lambda _pid: "new-start"
            )

            self.assertTrue(provider["ok"])
            self.assertEqual(sessions, [])

    def test_hook_prunes_dead_and_reused_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary), fingerprint="old-start")
            registry = home / status.REGISTRY_RELATIVE_PATH

            status.prune_registry(registry, fingerprint_lookup=lambda _pid: "new-start")

            self.assertEqual(list(registry.glob("*.json")), [])

    def test_malformed_record_makes_codex_coverage_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            _write_hooks(home, Path(status.__file__).resolve())
            registry = home / status.REGISTRY_RELATIVE_PATH
            registry.mkdir(parents=True)
            (registry / "broken.json").write_text("{not-json", encoding="utf-8")

            provider, sessions = status.collect_codex_sessions(home)

            self.assertFalse(provider["ok"])
            self.assertIn("invalid registry", provider["error"])
            self.assertEqual(sessions, [])

    def test_boolean_pid_makes_codex_coverage_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary))
            record_path = next((home / status.REGISTRY_RELATIVE_PATH).glob("*.json"))
            record = json.loads(record_path.read_text())
            record["pid"] = True
            record_path.write_text(json.dumps(record), encoding="utf-8")

            provider, sessions = status.collect_codex_sessions(
                home, fingerprint_lookup=lambda _pid: "fingerprint"
            )

            self.assertFalse(provider["ok"])
            self.assertIn("invalid registry", provider["error"])
            self.assertEqual(sessions, [])

    def test_missing_hook_registration_makes_codex_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")

            provider, sessions = status.collect_codex_sessions(home)

            self.assertFalse(provider["ok"])
            self.assertIn("not registered", provider["error"])
            self.assertEqual(sessions, [])

    def test_registration_requires_exact_hook_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            script_path = Path(status.__file__).resolve()
            handlers = {
                event: [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{script_path}.bak hook",
                            }
                        ]
                    }
                ]
                for event in status.HOOK_EVENTS
            }
            (home / "hooks.json").write_text(
                json.dumps({"hooks": handlers}), encoding="utf-8"
            )

            provider, sessions = status.collect_codex_sessions(
                home, script_path=script_path
            )

            self.assertFalse(provider["ok"])
            self.assertIn("not registered", provider["error"])
            self.assertEqual(sessions, [])


class TestClaudeNormalization(unittest.TestCase):
    def test_normalizes_live_states_and_filters_idle_completed(self) -> None:
        rows = [
            {
                "sessionId": "interactive-working",
                "cwd": "/tmp/a",
                "startedAt": 1785580837601,
                "status": "working",
                "pid": 10,
            },
            {
                "id": "background-blocked",
                "cwd": "/tmp/b",
                "startedAt": 1785580837602,
                "state": "blocked",
            },
            {
                "sessionId": "interactive-waiting",
                "cwd": "/tmp/c",
                "startedAt": 1785580837603,
                "status": "waiting",
                "pid": 11,
            },
            {
                "sessionId": "interactive-idle",
                "cwd": "/tmp/d",
                "startedAt": 1785580837604,
                "status": "idle",
                "pid": 12,
            },
            {
                "id": "background-done",
                "cwd": "/tmp/e",
                "startedAt": 1785580837605,
                "state": "done",
                "status": "waiting",
                "pid": 13,
            },
        ]

        sessions, errors = status.normalize_claude_sessions(rows)

        self.assertEqual(errors, [])
        self.assertEqual(
            [(row["session_id"], row["state"]) for row in sessions],
            [
                ("interactive-working", "working"),
                ("background-blocked", "waiting"),
                ("interactive-waiting", "waiting"),
            ],
        )
        self.assertIsNone(sessions[1]["pid"])

    def test_live_malformed_row_marks_provider_partial(self) -> None:
        rows = [
            {
                "cwd": "/tmp/a",
                "startedAt": 1785580837601,
                "status": "working",
            }
        ]

        sessions, errors = status.normalize_claude_sessions(rows)

        self.assertEqual(sessions, [])
        self.assertEqual(errors, ["Claude live row 0 has no session ID"])

    def test_unknown_state_marks_provider_partial(self) -> None:
        rows = [
            {
                "sessionId": "future-state",
                "cwd": "/tmp/a",
                "startedAt": 1785580837601,
                "status": "starting",
            }
        ]

        sessions, errors = status.normalize_claude_sessions(rows)

        self.assertEqual(sessions, [])
        self.assertEqual(errors, ["Claude row 0 has unsupported state/status"])

    def test_collect_claude_handles_command_failure(self) -> None:
        def runner(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 1, "", "daemon unavailable")

        provider, sessions = status.collect_claude_sessions(runner=runner)

        self.assertFalse(provider["ok"])
        self.assertEqual(provider["source"], status.CLAUDE_SOURCE)
        self.assertIn("daemon unavailable", provider["error"])
        self.assertEqual(sessions, [])

    def test_collect_claude_handles_invalid_json(self) -> None:
        def runner(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, "not-json", "")

        provider, sessions = status.collect_claude_sessions(runner=runner)

        self.assertFalse(provider["ok"])
        self.assertIn("invalid JSON", provider["error"])
        self.assertEqual(sessions, [])


class TestCli(unittest.TestCase):
    def test_hook_malformed_input_is_silent_nonblocking(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout):
            return_code = status.run_hook(stdin=StringIO("not-json"), stderr=stderr)

        self.assertEqual(return_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("agent-session-status:", stderr.getvalue())

    def test_hook_writes_nothing_to_stdout(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                status, "codex_process_identity", return_value=(123, "fingerprint")
            ),
            redirect_stdout(stdout),
        ):
            return_code = status.run_hook(
                stdin=StringIO(json.dumps(_prompt_event())),
                stderr=stderr,
                codex_home=Path(temporary),
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_json_output_and_complete_exit_code(self) -> None:
        output = StringIO()
        document = _complete_document()

        with patch.object(status, "build_inventory", return_value=document):
            return_code = status.run_status(json_output=True, stdout=output)

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(output.getvalue()), document)

    def test_human_output_is_compact(self) -> None:
        output = StringIO()

        with patch.object(status, "build_inventory", return_value=_complete_document()):
            return_code = status.run_status(json_output=False, stdout=output)

        self.assertEqual(return_code, 0)
        self.assertIn("CLIENT\tSTATE\tSESSION\tCWD", output.getvalue())
        self.assertIn("codex\tin_flight\tsession-1\t/tmp/project", output.getvalue())
        self.assertNotIn("WARNING", output.getvalue())

    def test_human_output_escapes_control_characters(self) -> None:
        document = _complete_document()
        document["sessions"][0]["cwd"] = (
            "/tmp/a\nclaude\tworking\tforged-session\t/tmp/b"
        )
        document["complete"] = False
        document["providers"]["claude"] = {
            "ok": False,
            "source": status.CLAUDE_SOURCE,
            "error": "failed\nCoverage: forged",
        }

        rendered = status._human_status(document)

        self.assertIn(
            "codex\tin_flight\tsession-1\t"
            "/tmp/a\\nclaude\\tworking\\tforged-session\\t/tmp/b",
            rendered,
        )
        self.assertIn("claude: failed\\nCoverage: forged", rendered)
        self.assertEqual(rendered.count("\nCoverage:"), 1)

    def test_partial_coverage_returns_two_with_usable_json(self) -> None:
        output = StringIO()
        document = _complete_document()
        document["complete"] = False
        document["providers"]["claude"] = {
            "ok": False,
            "source": status.CLAUDE_SOURCE,
            "error": "command unavailable",
        }

        with patch.object(status, "build_inventory", return_value=document):
            return_code = status.run_status(json_output=True, stdout=output)

        parsed = json.loads(output.getvalue())
        self.assertEqual(return_code, 2)
        self.assertFalse(parsed["complete"])
        self.assertEqual(parsed["sessions"][0]["session_id"], "session-1")

    def test_provider_exception_preserves_partial_inventory(self) -> None:
        claude_session = {
            "client": "claude",
            "session_id": "claude-1",
            "state": "waiting",
            "cwd": "/tmp/project",
            "pid": 123,
            "started_at": "2026-08-01T10:00:00.000Z",
        }
        with (
            patch.object(
                status,
                "collect_codex_sessions",
                side_effect=OSError("registry unreadable"),
            ),
            patch.object(
                status,
                "collect_claude_sessions",
                return_value=(
                    {"ok": True, "source": status.CLAUDE_SOURCE},
                    [claude_session],
                ),
            ),
        ):
            document = status.build_inventory()

        self.assertFalse(document["complete"])
        self.assertEqual(document["providers"]["codex"]["error"], "registry unreadable")
        self.assertEqual(document["sessions"], [claude_session])

    def test_partial_human_output_visibly_warns(self) -> None:
        output = StringIO()
        document = _complete_document()
        document["complete"] = False
        document["providers"]["claude"] = {
            "ok": False,
            "source": status.CLAUDE_SOURCE,
            "error": "command unavailable",
        }

        with patch.object(status, "build_inventory", return_value=document):
            return_code = status.run_status(json_output=False, stdout=output)

        self.assertEqual(return_code, 2)
        self.assertIn("WARNING: incomplete provider coverage", output.getvalue())
        self.assertIn("claude=unavailable", output.getvalue())

    def test_invalid_usage_returns_ex_usage(self) -> None:
        stderr = StringIO()

        with redirect_stderr(stderr):
            return_code = status.main(["status", "--unknown"])

        self.assertEqual(return_code, os.EX_USAGE)
        self.assertIn("usage:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
