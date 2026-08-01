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

from _agent_session_status import (  # noqa: E402, RUF100
    _core as core,
)
from _agent_session_status import (
    claude,
    cli,
    codex,
    inventory,
    process,
)
from _agent_session_status import (
    registry as storage,
)

SCRIPT_PATH = Path(__file__).with_name("agent_session_status.py").resolve()


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
        for event in core.HOOK_EVENTS
    }
    (home / "hooks.json").write_text(json.dumps({"hooks": handlers}), encoding="utf-8")


def _complete_document() -> dict:
    return {
        "schema_version": 1,
        "complete": True,
        "providers": {
            "codex": {"ok": True, "source": core.CODEX_SOURCE},
            "claude": {"ok": True, "source": core.CLAUDE_SOURCE},
        },
        "sessions": [
            {
                "client": "codex",
                "session_id": "session-1",
                "state": "in_flight",
                "cwd": "/tmp/project",
                "pid": 123,
                "started_at": "2026-08-01T10:00:00.000Z",
                "updated_at": "2026-08-01T10:00:00.000Z",
                "name": None,
                "waiting_for": None,
                "label": None,
                "age_seconds": 300,
            }
        ],
        "notes": {},
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
            patch.object(process.os, "getppid", return_value=100),
            patch.object(process.time, "monotonic", side_effect=monotonic),
            patch.object(process, "ps_value", side_effect=ps_value),
        ):
            identity = process.codex_process_identity(timeout_seconds=1.0)

        self.assertIsNone(identity)
        self.assertEqual(
            [field for _pid, field, _timeout in calls],
            ["comm", "command", "ppid"],
        )
        self.assertTrue(all(0 < timeout <= 1.0 for _pid, _field, timeout in calls))


class TestHookTransitions(unittest.TestCase):
    def test_prompt_writes_and_stop_removes_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            codex.handle_hook_event(
                _prompt_event(),
                registry_path,
                process_identity=(123, "fingerprint"),
                now="2026-08-01T10:00:00.000Z",
            )

            records = list(registry_path.glob("*.json"))
            self.assertEqual(len(records), 1)

            codex.handle_hook_event(
                _stop_event(),
                registry_path,
                fingerprint_lookup=lambda _pid: "fingerprint",
            )
            self.assertEqual(list(registry_path.glob("*.json")), [])

    def test_session_end_removes_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            codex.handle_hook_event(
                _prompt_event(),
                registry_path,
                process_identity=(123, "fingerprint"),
            )
            codex.handle_hook_event(
                _stop_event("SessionEnd"),
                registry_path,
                fingerprint_lookup=lambda _pid: "fingerprint",
            )

            self.assertEqual(list(registry_path.glob("*.json")), [])

    def test_duplicate_prompt_preserves_started_at(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            codex.handle_hook_event(
                _prompt_event(),
                registry_path,
                process_identity=(123, "fingerprint"),
                now="2026-08-01T10:00:00.000Z",
            )
            codex.handle_hook_event(
                _prompt_event(),
                registry_path,
                process_identity=(123, "fingerprint"),
                fingerprint_lookup=lambda _pid: "fingerprint",
                now="2026-08-01T10:01:00.000Z",
            )

            record = json.loads(next(registry_path.glob("*.json")).read_text())
            self.assertEqual(record["started_at"], "2026-08-01T10:00:00.000Z")
            self.assertEqual(record["updated_at"], "2026-08-01T10:01:00.000Z")

    def test_records_are_atomic_private_and_concurrent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"

            def write(index: int) -> None:
                codex.handle_hook_event(
                    _prompt_event(f"session-{index}", f"turn-{index}"),
                    registry_path,
                    process_identity=(1000 + index, f"fingerprint-{index}"),
                    fingerprint_lookup=lambda pid: f"fingerprint-{pid - 1000}",
                    now=f"2026-08-01T10:00:{index:02d}.000Z",
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(20)))

            records = list(registry_path.glob("*.json"))
            self.assertEqual(len(records), 20)
            self.assertEqual(stat.S_IMODE(registry_path.stat().st_mode), 0o700)
            for path in records:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(json.loads(path.read_text())["schema_version"], 1)
            self.assertEqual(list(registry_path.glob(".*.tmp-*")), [])

    def test_concurrent_replacement_never_leaves_partial_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"

            def write(index: int) -> None:
                codex.handle_hook_event(
                    _prompt_event("shared-session", "shared-turn"),
                    registry_path,
                    process_identity=(123, "fingerprint"),
                    fingerprint_lookup=lambda _pid: "fingerprint",
                    now=f"2026-08-01T10:00:{index:02d}.000Z",
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(20)))

            records = list(registry_path.glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(
                json.loads(records[0].read_text())["session_id"], "shared-session"
            )

    def test_record_excludes_private_hook_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            codex.handle_hook_event(
                _prompt_event(),
                registry_path,
                process_identity=(123, "fingerprint"),
            )

            raw = next(registry_path.glob("*.json")).read_text()
            record = json.loads(raw)
            self.assertEqual(set(record), storage.RECORD_FIELDS)
            self.assertNotIn("private prompt text", raw)
            self.assertNotIn("private-transcript", raw)
            self.assertNotIn("private assistant text", raw)
            self.assertNotIn("private tool data", raw)

    def test_prompt_is_recorded_before_pruning_other_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            codex.handle_hook_event(
                _prompt_event("old-session"),
                registry_path,
                process_identity=(123, "fingerprint"),
            )

            with self.assertRaisesRegex(RuntimeError, "lookup failed"):
                codex.handle_hook_event(
                    _prompt_event("new-session"),
                    registry_path,
                    process_identity=(124, "new-fingerprint"),
                    fingerprint_lookup=_raise_lookup_error,
                )

            session_ids = {
                json.loads(path.read_text())["session_id"]
                for path in registry_path.glob("*.json")
            }
            self.assertIn("new-session", session_ids)

    def test_stop_removes_record_before_pruning_other_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            for session_id in ("stopped-session", "other-session"):
                codex.handle_hook_event(
                    _prompt_event(session_id),
                    registry_path,
                    process_identity=(123, "fingerprint"),
                    fingerprint_lookup=lambda _pid: "fingerprint",
                )

            with self.assertRaisesRegex(RuntimeError, "lookup failed"):
                codex.handle_hook_event(
                    _stop_event(session_id="stopped-session"),
                    registry_path,
                    fingerprint_lookup=_raise_lookup_error,
                )

            session_ids = {
                json.loads(path.read_text())["session_id"]
                for path in registry_path.glob("*.json")
            }
            self.assertNotIn("stopped-session", session_ids)


class TestRegistryLiveness(unittest.TestCase):
    def _create_home(self, root: Path, fingerprint: str = "fingerprint") -> Path:
        home = root / "codex-home"
        home.mkdir()
        _write_hooks(home, SCRIPT_PATH)
        registry_path = home / core.REGISTRY_RELATIVE_PATH
        codex.handle_hook_event(
            _prompt_event(),
            registry_path,
            process_identity=(123, fingerprint),
        )
        return home

    def test_matching_process_fingerprint_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary))

            provider, sessions = codex.collect_sessions(
                home, fingerprint_lookup=lambda _pid: "fingerprint"
            )

            self.assertTrue(provider["ok"])
            self.assertEqual([row["session_id"] for row in sessions], ["session-1"])

    def test_dead_process_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary))

            provider, sessions = codex.collect_sessions(
                home, fingerprint_lookup=lambda _pid: None
            )

            self.assertTrue(provider["ok"])
            self.assertEqual(sessions, [])

    def test_reused_pid_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary), fingerprint="old-start")

            provider, sessions = codex.collect_sessions(
                home, fingerprint_lookup=lambda _pid: "new-start"
            )

            self.assertTrue(provider["ok"])
            self.assertEqual(sessions, [])

    def test_hook_prunes_dead_and_reused_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary), fingerprint="old-start")
            registry_path = home / core.REGISTRY_RELATIVE_PATH

            storage.prune_registry(
                registry_path, fingerprint_lookup=lambda _pid: "new-start"
            )

            self.assertEqual(list(registry_path.glob("*.json")), [])

    def test_malformed_record_makes_codex_coverage_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            _write_hooks(home, SCRIPT_PATH)
            registry_path = home / core.REGISTRY_RELATIVE_PATH
            registry_path.mkdir(parents=True)
            (registry_path / "broken.json").write_text("{not-json", encoding="utf-8")

            provider, sessions = codex.collect_sessions(home)

            self.assertFalse(provider["ok"])
            self.assertIn("invalid registry", provider["error"])
            self.assertEqual(sessions, [])

    def test_boolean_pid_makes_codex_coverage_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._create_home(Path(temporary))
            record_path = next((home / core.REGISTRY_RELATIVE_PATH).glob("*.json"))
            record = json.loads(record_path.read_text())
            record["pid"] = True
            record_path.write_text(json.dumps(record), encoding="utf-8")

            provider, sessions = codex.collect_sessions(
                home, fingerprint_lookup=lambda _pid: "fingerprint"
            )

            self.assertFalse(provider["ok"])
            self.assertIn("invalid registry", provider["error"])
            self.assertEqual(sessions, [])

    def test_missing_hook_registration_makes_codex_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")

            provider, sessions = codex.collect_sessions(home)

            self.assertFalse(provider["ok"])
            self.assertIn("not registered", provider["error"])
            self.assertEqual(sessions, [])

    def test_registration_requires_exact_hook_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            script_path = SCRIPT_PATH
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
                for event in core.HOOK_EVENTS
            }
            (home / "hooks.json").write_text(
                json.dumps({"hooks": handlers}), encoding="utf-8"
            )

            provider, sessions = codex.collect_sessions(home, script_path=script_path)

            self.assertFalse(provider["ok"])
            self.assertIn("not registered", provider["error"])
            self.assertEqual(sessions, [])


class TestClaudeNormalization(unittest.TestCase):
    def test_normalizes_live_states_including_idle_and_filters_terminal(self) -> None:
        rows = [
            {
                "sessionId": "interactive-working",
                "cwd": "/tmp/a",
                "startedAt": 1785580837601,
                "status": "working",
                "pid": 10,
            },
            {
                "sessionId": "interactive-busy",
                "cwd": "/tmp/f",
                "startedAt": 1785580837600,
                "status": "busy",
                "pid": 14,
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
                "name": "stale-test-cleanup",
                "waitingFor": "input",
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
            {
                "sessionId": "background-stopped",
                "cwd": "/tmp/g",
                "startedAt": 1785580837606,
                "status": "stopped",
                "pid": 15,
            },
        ]

        sessions, errors = claude.normalize_sessions(rows)

        self.assertEqual(errors, [])
        self.assertEqual(
            [(row["session_id"], row["state"]) for row in sessions],
            [
                ("interactive-working", "working"),
                ("interactive-busy", "working"),
                ("background-blocked", "waiting"),
                ("interactive-waiting", "waiting"),
                ("interactive-idle", "idle"),
            ],
        )
        self.assertIsNone(sessions[2]["pid"])
        waiting_session = sessions[3]
        self.assertEqual(waiting_session["name"], "stale-test-cleanup")
        self.assertEqual(waiting_session["waiting_for"], "input")
        idle_session = sessions[4]
        self.assertIsNone(idle_session["name"])
        self.assertIsNone(idle_session["waiting_for"])

    def test_live_malformed_row_marks_provider_partial(self) -> None:
        rows = [
            {
                "cwd": "/tmp/a",
                "startedAt": 1785580837601,
                "status": "working",
            }
        ]

        sessions, errors = claude.normalize_sessions(rows)

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

        sessions, errors = claude.normalize_sessions(rows)

        self.assertEqual(sessions, [])
        self.assertEqual(errors, ["Claude row 0 has unsupported state/status"])

    def test_timezone_less_started_at_marks_provider_partial(self) -> None:
        rows = [
            {
                "sessionId": "missing-timezone",
                "cwd": "/tmp/a",
                "startedAt": "2026-08-01T10:00:00",
                "status": "working",
            }
        ]

        sessions, errors = claude.normalize_sessions(rows)

        self.assertEqual(sessions, [])
        self.assertEqual(errors, ["Claude live row 0 has no valid startedAt"])

    def test_collect_claude_handles_command_failure(self) -> None:
        def runner(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 1, "", "daemon unavailable")

        provider, sessions = claude.collect_sessions(runner=runner)

        self.assertFalse(provider["ok"])
        self.assertEqual(provider["source"], core.CLAUDE_SOURCE)
        self.assertIn("daemon unavailable", provider["error"])
        self.assertEqual(sessions, [])

    def test_collect_claude_handles_invalid_json(self) -> None:
        def runner(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, "not-json", "")

        provider, sessions = claude.collect_sessions(runner=runner)

        self.assertFalse(provider["ok"])
        self.assertIn("invalid JSON", provider["error"])
        self.assertEqual(sessions, [])


class TestCli(unittest.TestCase):
    def test_executable_shim_dispatches_to_cli(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "bogus"],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, os.EX_USAGE)
        self.assertIn("usage:", result.stderr)

    def test_hook_malformed_input_is_silent_nonblocking(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout):
            return_code = codex.run_hook(stdin=StringIO("not-json"), stderr=stderr)

        self.assertEqual(return_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("agent-session-status:", stderr.getvalue())

    def test_hook_writes_nothing_to_stdout(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                codex, "codex_process_identity", return_value=(123, "fingerprint")
            ),
            redirect_stdout(stdout),
        ):
            return_code = codex.run_hook(
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

        with patch.object(inventory, "build_inventory", return_value=document):
            return_code = cli.run_status(json_output=True, stdout=output)

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(output.getvalue()), document)

    def test_human_output_is_compact(self) -> None:
        output = StringIO()

        with patch.object(
            inventory, "build_inventory", return_value=_complete_document()
        ):
            return_code = cli.run_status(json_output=False, stdout=output)

        self.assertEqual(return_code, 0)
        self.assertIn("CLIENT\tSTATE\tAGE\tNAME/LABEL\tSESSION\tCWD", output.getvalue())
        self.assertIn(
            "codex\tin_flight\t5m\t\tsession-1\t/tmp/project", output.getvalue()
        )
        self.assertNotIn("WARNING", output.getvalue())

    def test_human_output_escapes_control_characters(self) -> None:
        document = _complete_document()
        document["sessions"][0]["cwd"] = (
            "/tmp/a\nclaude\tworking\tforged-session\t/tmp/b"
        )
        document["complete"] = False
        document["providers"]["claude"] = {
            "ok": False,
            "source": core.CLAUDE_SOURCE,
            "error": "failed\nCoverage: forged",
        }

        rendered = inventory.human_status(document)

        self.assertIn(
            "codex\tin_flight\t5m\t\tsession-1\t"
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
            "source": core.CLAUDE_SOURCE,
            "error": "command unavailable",
        }

        with patch.object(inventory, "build_inventory", return_value=document):
            return_code = cli.run_status(json_output=True, stdout=output)

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
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                codex,
                "collect_sessions",
                side_effect=OSError("registry unreadable"),
            ),
            patch.object(
                claude,
                "collect_sessions",
                return_value=(
                    {"ok": True, "source": core.CLAUDE_SOURCE},
                    [claude_session],
                ),
            ),
        ):
            document = inventory.build_inventory(codex_home=Path(temporary))

        self.assertFalse(document["complete"])
        self.assertEqual(document["providers"]["codex"]["error"], "registry unreadable")
        self.assertEqual(len(document["sessions"]), 1)
        result_session = document["sessions"][0]
        self.assertEqual(result_session["session_id"], "claude-1")
        self.assertEqual(result_session["client"], "claude")
        self.assertIsNone(result_session["label"])

    def test_partial_human_output_visibly_warns(self) -> None:
        output = StringIO()
        document = _complete_document()
        document["complete"] = False
        document["providers"]["claude"] = {
            "ok": False,
            "source": core.CLAUDE_SOURCE,
            "error": "command unavailable",
        }

        with patch.object(inventory, "build_inventory", return_value=document):
            return_code = cli.run_status(json_output=False, stdout=output)

        self.assertEqual(return_code, 2)
        self.assertIn("WARNING: incomplete provider coverage", output.getvalue())
        self.assertIn("claude=unavailable", output.getvalue())

    def test_invalid_usage_returns_ex_usage(self) -> None:
        stderr = StringIO()

        with redirect_stderr(stderr):
            return_code = cli.main(["status", "--unknown"])

        self.assertEqual(return_code, os.EX_USAGE)
        self.assertIn("usage:", stderr.getvalue())

    def test_unknown_verb_returns_ex_usage(self) -> None:
        stderr = StringIO()

        with redirect_stderr(stderr):
            return_code = cli.main(["bogus"])

        self.assertEqual(return_code, os.EX_USAGE)

    def test_no_arguments_returns_ex_usage(self) -> None:
        stderr = StringIO()

        with redirect_stderr(stderr):
            return_code = cli.main([])

        self.assertEqual(return_code, os.EX_USAGE)


class TestAge(unittest.TestCase):
    def test_age_seconds_computes_delta(self) -> None:
        seconds = core.age_seconds(
            "2026-08-01T10:00:00.000Z", "2026-08-01T10:05:00.000Z"
        )
        self.assertEqual(seconds, 300)

    def test_age_seconds_clamps_negative_to_zero(self) -> None:
        seconds = core.age_seconds(
            "2026-08-01T10:05:00.000Z", "2026-08-01T10:00:00.000Z"
        )
        self.assertEqual(seconds, 0)

    def test_age_seconds_returns_none_for_malformed_timestamp(self) -> None:
        self.assertIsNone(
            core.age_seconds("not-a-timestamp", "2026-08-01T10:00:00.000Z")
        )

    def test_age_seconds_returns_none_for_timezone_less_timestamp(self) -> None:
        self.assertIsNone(
            core.age_seconds("2026-08-01T10:00:00", "2026-08-01T10:05:00.000Z")
        )

    def test_format_age_thresholds(self) -> None:
        self.assertEqual(core.format_age(None), "")
        self.assertEqual(core.format_age(45), "45s")
        self.assertEqual(core.format_age(240), "4m")
        self.assertEqual(core.format_age(3 * 3600), "3h")
        self.assertEqual(core.format_age(2 * 24 * 3600), "2d")


class TestProcessAncestors(unittest.TestCase):
    def test_stops_at_pid_one(self) -> None:
        def ps_value(pid: int, field: str, _timeout_seconds: float) -> str:
            self.assertEqual(field, "ppid")
            return str(max(pid - 1, 0))

        chain = process.process_ancestors(3, ps_value=ps_value)

        self.assertEqual(chain, [3, 2])

    def test_bounded_by_timeout(self) -> None:
        clock = [0.0]

        def monotonic() -> float:
            clock[0] += 0.2
            return clock[0]

        def ps_value(pid: int, _field: str, _timeout_seconds: float) -> str:
            return str(pid - 1)

        with patch.object(process.time, "monotonic", side_effect=monotonic):
            chain = process.process_ancestors(
                1000, timeout_seconds=1.0, ps_value=ps_value
            )

        self.assertLessEqual(len(chain), 16)
        self.assertTrue(all(pid > 0 for pid in chain))


class TestResolveIdentity(unittest.TestCase):
    def test_empty_ancestor_chain_returns_none(self) -> None:
        self.assertIsNone(inventory.resolve_identity(ancestors=[]))

    def test_resolves_codex_session_by_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            home.mkdir()
            _write_hooks(home, SCRIPT_PATH)
            registry_path = home / core.REGISTRY_RELATIVE_PATH
            codex.handle_hook_event(
                _prompt_event("session-1"), registry_path, process_identity=(555, "fp")
            )

            def claude_runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 0, "[]", "")

            identity = inventory.resolve_identity(
                codex_home=home,
                fingerprint_lookup=lambda _pid: "fp",
                claude_runner=claude_runner,
                ancestors=[999, 555, 1],
            )

            self.assertEqual(identity, {"client": "codex", "session_id": "session-1"})

    def test_resolves_claude_session_by_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            home.mkdir()
            _write_hooks(home, SCRIPT_PATH)
            rows = [
                {
                    "sessionId": "claude-7",
                    "cwd": "/tmp/x",
                    "startedAt": 1785580837601,
                    "status": "working",
                    "pid": 777,
                }
            ]

            def claude_runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 0, json.dumps(rows), "")

            identity = inventory.resolve_identity(
                codex_home=home, claude_runner=claude_runner, ancestors=[888, 777]
            )

            self.assertEqual(identity, {"client": "claude", "session_id": "claude-7"})

    def test_unresolvable_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            home.mkdir()
            _write_hooks(home, SCRIPT_PATH)

            def claude_runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 0, "[]", "")

            identity = inventory.resolve_identity(
                codex_home=home, claude_runner=claude_runner, ancestors=[42, 43]
            )

            self.assertIsNone(identity)


class TestIdentityCli(unittest.TestCase):
    def test_prints_client_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            stdout = StringIO()

            return_code = cli.run_identity(
                codex_home=home,
                identity={"client": "codex", "session_id": "session-1"},
                stdout=stdout,
            )

            self.assertEqual(return_code, 0)
            self.assertEqual(stdout.getvalue(), "client=codex session=session-1\n")

    def test_includes_label_when_claim_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            registry_path = core.registry_dir(home)
            storage.write_claim(
                registry_path,
                session_id="session-1",
                client="codex",
                cwd="/tmp/project",
                label="convert pacing tests",
            )
            stdout = StringIO()

            cli.run_identity(
                codex_home=home,
                identity={"client": "codex", "session_id": "session-1"},
                stdout=stdout,
            )

            self.assertEqual(
                stdout.getvalue(),
                "client=codex session=session-1 label=convert pacing tests\n",
            )

    def test_escapes_control_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            registry_path = core.registry_dir(home)
            storage.write_claim(
                registry_path,
                session_id="session-1",
                client="codex",
                cwd="/tmp/project",
                label="legit\nclient=forged",
            )
            stdout = StringIO()

            cli.run_identity(
                codex_home=home,
                identity={"client": "codex", "session_id": "session-1"},
                stdout=stdout,
            )

            self.assertEqual(
                stdout.getvalue(),
                "client=codex session=session-1 label=legit\\nclient=forged\n",
            )

    def test_unresolvable_identity_exits_one(self) -> None:
        stderr = StringIO()

        with patch.object(inventory, "resolve_identity", return_value=None):
            return_code = cli.run_identity(
                codex_home=Path("/nonexistent"), stderr=stderr
            )

        self.assertEqual(return_code, 1)
        self.assertIn("agent-session-status:", stderr.getvalue())


class TestClaimCli(unittest.TestCase):
    def test_override_writes_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"

            return_code = cli.run_claim(
                "convert pacing tests to TestClock",
                codex_home=home,
                client_override="codex",
                session_override="session-9",
                cwd="/repo",
                now="2026-08-01T10:00:00.000Z",
            )

            self.assertEqual(return_code, 0)
            claims_dir = core.registry_dir(home) / "claims"
            files = list(claims_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            record = json.loads(files[0].read_text())
            self.assertEqual(
                record,
                {
                    "schema_version": 1,
                    "session_id": "session-9",
                    "client": "codex",
                    "cwd": "/repo",
                    "label": "convert pacing tests to TestClock",
                    "created_at": "2026-08-01T10:00:00.000Z",
                },
            )

    def test_uses_injected_identity_when_no_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"

            return_code = cli.run_claim(
                "label",
                codex_home=home,
                identity={"client": "claude", "session_id": "claude-1"},
            )

            self.assertEqual(return_code, 0)
            claims = storage.load_claims(core.registry_dir(home))
            self.assertIn(("claude", "claude-1"), claims)

    def test_unresolvable_identity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            stderr = StringIO()

            with patch.object(inventory, "resolve_identity", return_value=None):
                return_code = cli.run_claim("label", codex_home=home, stderr=stderr)

            self.assertEqual(return_code, 1)
            self.assertEqual(storage.load_claims(core.registry_dir(home)), {})


class TestClaimArgParsing(unittest.TestCase):
    def test_parses_label_only(self) -> None:
        self.assertEqual(
            cli._parse_claim_args(["do the thing"]), ("do the thing", None, None)
        )

    def test_parses_override_flags_before_label(self) -> None:
        parsed = cli._parse_claim_args(
            ["--client", "claude", "--session", "abc", "label text"]
        )
        self.assertEqual(parsed, ("label text", "claude", "abc"))

    def test_rejects_missing_label(self) -> None:
        self.assertIsNone(cli._parse_claim_args([]))

    def test_rejects_partial_override(self) -> None:
        self.assertIsNone(cli._parse_claim_args(["--client", "codex", "label"]))

    def test_rejects_invalid_client(self) -> None:
        self.assertIsNone(
            cli._parse_claim_args(["--client", "bogus", "--session", "x", "label"])
        )

    def test_rejects_empty_session_override(self) -> None:
        self.assertIsNone(
            cli._parse_claim_args(["--client", "codex", "--session", "", "label"])
        )

    def test_rejects_multiple_positionals(self) -> None:
        self.assertIsNone(cli._parse_claim_args(["one", "two"]))


class TestClaimPruning(unittest.TestCase):
    def test_prune_removes_claim_when_codex_session_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            codex.handle_hook_event(
                _prompt_event("session-1"),
                registry_path,
                process_identity=(123, "fingerprint"),
            )
            storage.write_claim(
                registry_path,
                session_id="session-1",
                client="codex",
                cwd="/tmp/project",
                label="doing work",
            )

            storage.prune_registry(registry_path, fingerprint_lookup=lambda _pid: None)

            self.assertEqual(list((registry_path / "claims").glob("*.json")), [])

    def test_prune_keeps_claim_when_codex_session_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            codex.handle_hook_event(
                _prompt_event("session-1"),
                registry_path,
                process_identity=(123, "fingerprint"),
            )
            storage.write_claim(
                registry_path,
                session_id="session-1",
                client="codex",
                cwd="/tmp/project",
                label="doing work",
            )

            storage.prune_registry(
                registry_path, fingerprint_lookup=lambda _pid: "fingerprint"
            )

            self.assertEqual(len(list((registry_path / "claims").glob("*.json"))), 1)

    def test_prune_leaves_claude_claim_when_liveness_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            storage.write_claim(
                registry_path,
                session_id="claude-9",
                client="claude",
                cwd="/tmp/project",
                label="doing work",
            )

            storage.prune_registry(registry_path)

            self.assertEqual(len(list((registry_path / "claims").glob("*.json"))), 1)

    def test_prune_removes_dead_claude_claim_when_ids_given(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            storage.write_claim(
                registry_path,
                session_id="claude-9",
                client="claude",
                cwd="/tmp/project",
                label="doing work",
            )

            storage.prune_registry(
                registry_path, claude_session_ids=frozenset({"claude-other"})
            )

            self.assertEqual(list((registry_path / "claims").glob("*.json")), [])

    def test_prune_keeps_live_claude_claim_when_ids_given(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            storage.write_claim(
                registry_path,
                session_id="claude-9",
                client="claude",
                cwd="/tmp/project",
                label="doing work",
            )

            storage.prune_registry(
                registry_path, claude_session_ids=frozenset({"claude-9"})
            )

            self.assertEqual(len(list((registry_path / "claims").glob("*.json"))), 1)


class TestNotes(unittest.TestCase):
    def test_add_and_load_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"

            entry = storage.add_note(
                registry_path,
                "/repo",
                "finding",
                session_id="s1",
                client="codex",
                now="2026-08-01T10:00:00.000Z",
            )
            notes = storage.load_notes(registry_path, now="2026-08-01T10:05:00.000Z")

            self.assertEqual(list(notes.keys()), ["/repo"])
            self.assertEqual(notes["/repo"][0]["id"], entry["id"])
            self.assertEqual(notes["/repo"][0]["text"], "finding")
            self.assertEqual(notes["/repo"][0]["session_id"], "s1")
            self.assertEqual(notes["/repo"][0]["client"], "codex")
            self.assertEqual(notes["/repo"][0]["age_seconds"], 300)

    def test_remove_note_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            entry = storage.add_note(registry_path, "/repo", "finding")

            removed = storage.remove_note(registry_path, "/repo", entry["id"])

            self.assertTrue(removed)
            self.assertEqual(storage.load_notes(registry_path), {})

    def test_same_millisecond_notes_have_distinct_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            with patch.object(
                storage,
                "note_id",
                side_effect=["00000000", "00000000", "11111111"],
            ):
                first = storage.add_note(
                    registry_path,
                    "/repo",
                    "same",
                    now="2026-08-01T10:00:00.000Z",
                )
                second = storage.add_note(
                    registry_path,
                    "/repo",
                    "same",
                    now="2026-08-01T10:00:00.000Z",
                )

            self.assertNotEqual(first["id"], second["id"])
            self.assertTrue(storage.remove_note(registry_path, "/repo", first["id"]))
            notes = storage.load_notes(registry_path, now="2026-08-01T10:00:01.000Z")
            self.assertEqual([entry["id"] for entry in notes["/repo"]], [second["id"]])

    def test_concurrent_adds_preserve_all_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"

            def add(index: int) -> None:
                storage.add_note(
                    registry_path,
                    "/repo",
                    f"note-{index}",
                    now=f"2026-08-01T10:00:{index:02d}.000Z",
                )

            with ThreadPoolExecutor(max_workers=20) as executor:
                list(executor.map(add, range(20)))

            notes = storage.load_notes(registry_path, now="2026-08-01T10:01:00.000Z")
            self.assertEqual(
                {entry["text"] for entry in notes["/repo"]},
                {f"note-{index}" for index in range(20)},
            )
            lock_files = list((registry_path / "notes").glob(".*.lock"))
            self.assertEqual(len(lock_files), 1)
            self.assertEqual(stat.S_IMODE(lock_files[0].stat().st_mode), 0o600)

    def test_remove_unknown_note_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            storage.add_note(registry_path, "/repo", "finding")

            self.assertFalse(storage.remove_note(registry_path, "/repo", "deadbeef"))

    def test_notes_expire_after_seven_days(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            storage.add_note(
                registry_path, "/repo", "old", now="2026-07-24T10:00:00.000Z"
            )
            storage.add_note(
                registry_path, "/repo", "new", now="2026-07-31T10:00:00.000Z"
            )

            notes = storage.load_notes(registry_path, now="2026-08-01T10:00:00.000Z")

            self.assertEqual([entry["text"] for entry in notes["/repo"]], ["new"])

    def test_expired_notes_pruned_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry"
            storage.add_note(
                registry_path, "/repo", "old", now="2026-07-01T10:00:00.000Z"
            )

            storage.load_notes(registry_path, now="2026-08-01T10:00:00.000Z")

            self.assertEqual(list((registry_path / "notes").glob("*.json")), [])


class TestRepoRoot(unittest.TestCase):
    def test_preserves_trailing_spaces(self) -> None:
        def runner(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, "/repo \n", "")

        self.assertEqual(storage.repo_root("/fallback", runner=runner), "/repo ")


class TestNoteCli(unittest.TestCase):
    def test_add_writes_entry_with_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            stdout = StringIO()

            def repo_runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 0, "/repo\n", "")

            return_code = cli.run_note(
                text="finding",
                done_id=None,
                codex_home=home,
                identity={"client": "codex", "session_id": "abc"},
                repo_runner=repo_runner,
                stdout=stdout,
                now="2026-08-01T10:00:00.000Z",
            )

            self.assertEqual(return_code, 0)
            note_id = stdout.getvalue().strip()
            self.assertTrue(note_id)
            notes = storage.load_notes(
                core.registry_dir(home), now="2026-08-01T10:00:00.000Z"
            )
            self.assertEqual(notes["/repo"][0]["session_id"], "abc")
            self.assertEqual(notes["/repo"][0]["client"], "codex")

    def test_add_falls_back_to_cwd_when_not_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"

            def repo_runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 128, "", "not a git repository")

            cli.run_note(
                text="finding",
                done_id=None,
                codex_home=home,
                cwd="/not/a/repo",
                identity=None,
                repo_runner=repo_runner,
                stdout=StringIO(),
            )

            notes = storage.load_notes(core.registry_dir(home))
            self.assertIn("/not/a/repo", notes)

    def test_done_removes_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"

            def repo_runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 0, "/repo\n", "")

            add_stdout = StringIO()
            cli.run_note(
                text="finding",
                done_id=None,
                codex_home=home,
                repo_runner=repo_runner,
                stdout=add_stdout,
            )
            note_id = add_stdout.getvalue().strip()

            return_code = cli.run_note(
                text=None, done_id=note_id, codex_home=home, repo_runner=repo_runner
            )

            self.assertEqual(return_code, 0)
            self.assertEqual(storage.load_notes(core.registry_dir(home)), {})

    def test_done_missing_id_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            stderr = StringIO()

            def repo_runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 0, "/repo\n", "")

            return_code = cli.run_note(
                text=None,
                done_id="missing",
                codex_home=home,
                repo_runner=repo_runner,
                stderr=stderr,
            )

            self.assertEqual(return_code, 1)
            self.assertIn("agent-session-status:", stderr.getvalue())


class TestNoteArgParsing(unittest.TestCase):
    def test_parses_text(self) -> None:
        self.assertEqual(cli._parse_note_args(["a finding"]), ("a finding", None))

    def test_parses_done(self) -> None:
        self.assertEqual(cli._parse_note_args(["--done", "abc123"]), (None, "abc123"))

    def test_rejects_empty_args(self) -> None:
        self.assertIsNone(cli._parse_note_args([]))

    def test_rejects_too_many_args(self) -> None:
        self.assertIsNone(cli._parse_note_args(["a", "b", "c"]))


class TestClaimAndNoteMainDispatch(unittest.TestCase):
    def test_main_dispatches_claim(self) -> None:
        with patch.object(cli, "run_claim", return_value=0) as run_claim:
            return_code = cli.main(
                ["claim", "--client", "codex", "--session", "x", "label"]
            )

        self.assertEqual(return_code, 0)
        run_claim.assert_called_once_with(
            "label", client_override="codex", session_override="x"
        )

    def test_main_claim_bad_usage(self) -> None:
        with redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["claim"]), os.EX_USAGE)

    def test_main_dispatches_note(self) -> None:
        with patch.object(cli, "run_note", return_value=0) as run_note:
            return_code = cli.main(["note", "a finding"])

        self.assertEqual(return_code, 0)
        run_note.assert_called_once_with(text="a finding", done_id=None)

    def test_main_note_bad_usage(self) -> None:
        with redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["note"]), os.EX_USAGE)

    def test_main_dispatches_identity(self) -> None:
        with patch.object(cli, "run_identity", return_value=0) as run_identity:
            return_code = cli.main(["identity"])

        self.assertEqual(return_code, 0)
        run_identity.assert_called_once_with()

    def test_main_identity_rejects_extra_args(self) -> None:
        with redirect_stderr(StringIO()):
            self.assertEqual(cli.main(["identity", "extra"]), os.EX_USAGE)


class TestStatusWithLabelsAndNotes(unittest.TestCase):
    def test_status_text_and_json_include_label_age_and_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            home.mkdir()
            _write_hooks(home, SCRIPT_PATH)
            registry_path = home / core.REGISTRY_RELATIVE_PATH
            codex.handle_hook_event(
                _prompt_event("session-1"),
                registry_path,
                process_identity=(123, "fingerprint"),
                now="2026-08-01T09:55:00.000Z",
            )
            storage.write_claim(
                registry_path,
                session_id="session-1",
                client="codex",
                cwd="/tmp/project",
                label="convert pacing tests",
                now="2026-08-01T09:55:00.000Z",
            )
            storage.add_note(
                registry_path,
                "/tmp/project",
                "COREBTC staleness pre-exists",
                now="2026-08-01T09:00:00.000Z",
            )
            empty_claude = ({"ok": True, "source": core.CLAUDE_SOURCE}, [])

            with patch.object(claude, "collect_sessions", return_value=empty_claude):
                output = StringIO()
                return_code = cli.run_status(
                    json_output=False,
                    stdout=output,
                    codex_home=home,
                    now="2026-08-01T10:00:00.000Z",
                    fingerprint_lookup=lambda _pid: "fingerprint",
                )

            self.assertEqual(return_code, 0)
            rendered = output.getvalue()
            self.assertIn("CLIENT\tSTATE\tAGE\tNAME/LABEL\tSESSION\tCWD", rendered)
            self.assertIn(
                "codex\tin_flight\t5m\tconvert pacing tests\tsession-1\t/tmp/project",
                rendered,
            )
            self.assertIn("Notes (/tmp/project):", rendered)
            self.assertIn("COREBTC staleness pre-exists", rendered)

            with patch.object(claude, "collect_sessions", return_value=empty_claude):
                json_output = StringIO()
                cli.run_status(
                    json_output=True,
                    stdout=json_output,
                    codex_home=home,
                    now="2026-08-01T10:00:00.000Z",
                    fingerprint_lookup=lambda _pid: "fingerprint",
                )

            document = json.loads(json_output.getvalue())
            session = document["sessions"][0]
            self.assertEqual(session["label"], "convert pacing tests")
            self.assertEqual(session["age_seconds"], 300)
            self.assertIn("waiting_for", session)
            self.assertIn("name", session)
            self.assertEqual(
                document["notes"]["/tmp/project"][0]["text"],
                "COREBTC staleness pre-exists",
            )


if __name__ == "__main__":
    unittest.main()
