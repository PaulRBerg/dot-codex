#!/usr/bin/env -S uv run python
"""Unit tests for the Codex prompt clipboard hook."""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

import copy_prompt_to_clipboard as hook  # noqa: E402


def _long_prompt() -> str:
    return "x" * hook.MIN_PROMPT_CHARS


class TestSanitizePrompt(unittest.TestCase):
    def test_preserves_normal_prompt(self) -> None:
        self.assertEqual(hook.sanitize_prompt("hello **world**"), "hello **world**")

    def test_preserves_non_ascii(self) -> None:
        self.assertEqual(hook.sanitize_prompt("café — 日本語 → ✓"), "café — 日本語 → ✓")

    def test_normalizes_claude_markers(self) -> None:
        cases = {
            "[Pasted text #1 +50 lines]": "Pasted",
            "see [Image #2] here": "see Pasted here",
            "[...Truncated text #3 +9 lines...]": "Pasted",
        }

        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(hook.sanitize_prompt(prompt), expected)

    def test_normalizes_codex_markers(self) -> None:
        cases = {
            "[Pasted Content 123 chars]": "Pasted",
            "[Pasted Content 1,234 chars]": "Pasted",
            "see [codex-clipboard-abc123.png 640x480] here": "see Pasted here",
        }

        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(hook.sanitize_prompt(prompt), expected)

    def test_strips_triple_backtick_fence(self) -> None:
        result = hook.sanitize_prompt("before\n```ts\nconst x = 1;\n```\nafter")

        self.assertEqual(result, "before\n[code]\nafter")
        self.assertNotIn("const x", result)

    def test_strips_quad_backtick_fence(self) -> None:
        result = hook.sanitize_prompt("````md\n```\nnested\n```\n````")

        self.assertEqual(result, "[code]")
        self.assertNotIn("nested", result)

    def test_strips_unterminated_fence(self) -> None:
        result = hook.sanitize_prompt("see this:\n```python\nimport os\nprint(os)")

        self.assertEqual(result, "see this:\n[code]")
        self.assertNotIn("import os", result)

    def test_collapses_long_line(self) -> None:
        long_line = "x" * (hook.LONG_LINE_CHARS + 1)

        self.assertEqual(hook.sanitize_prompt(long_line), "[Pasted]")

    def test_collapses_over_cap_prompt(self) -> None:
        prompt = "\n".join(f"line{i}" for i in range(50))
        result = hook.sanitize_prompt(prompt)

        self.assertTrue(result.endswith("[Pasted]"))
        self.assertIn("line0", result)
        self.assertNotIn("line20", result)
        self.assertNotIn("line49", result)

    def test_squeezes_blank_lines(self) -> None:
        self.assertEqual(hook.sanitize_prompt("a\n\n\n\n\nb"), "a\n\nb")

    def test_empty_after_sanitize_returns_empty(self) -> None:
        self.assertEqual(hook.sanitize_prompt("   \n\n   "), "")

    def test_many_blank_lines_return_empty(self) -> None:
        self.assertEqual(hook.sanitize_prompt("\n" * (hook.MAX_LINES + 10)), "")


class TestMetadataPrefix(unittest.TestCase):
    def test_builds_metadata_with_thread_id(self) -> None:
        data = {
            "cwd": "/tmp/work/my repo/subdir",
            "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53",
        }

        with patch.object(hook, "_git_output", side_effect=["/tmp/work/my repo", ""]):
            self.assertEqual(
                hook.build_metadata_prefix(data),
                "[repo:my-repo thread:0199a213]",
            )

    def test_prefers_remote_repo_name(self) -> None:
        data = {
            "cwd": "/tmp/work/.codex",
            "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53",
        }

        with patch.object(
            hook,
            "_git_output",
            side_effect=[
                "/tmp/work/.codex",
                "git@github.com:PaulRBerg/dot-codex.git",
            ],
        ):
            self.assertEqual(
                hook.build_metadata_prefix(data),
                "[repo:dot-codex thread:0199a213]",
            )

    def test_builds_metadata_with_nested_thread_id(self) -> None:
        data = {
            "cwd": "/tmp/work/demo",
            "thread": {"threadId": "thr_1234567890"},
        }

        with patch.object(hook, "_git_output", side_effect=["/tmp/work/demo", ""]):
            self.assertEqual(
                hook.build_metadata_prefix(data),
                "[repo:demo thread:thr_1234]",
            )

    def test_builds_metadata_with_git_ref_when_thread_missing(self) -> None:
        data = {"cwd": "/tmp/work/demo"}

        with patch.object(
            hook,
            "_git_output",
            side_effect=["/tmp/work/demo", "", "dd66016a"],
        ):
            self.assertEqual(
                hook.build_metadata_prefix(data),
                "[repo:demo ref:dd66016a]",
            )

    def test_builds_metadata_with_path_ref_when_git_unavailable(self) -> None:
        data = {"cwd": "/tmp/work/demo"}

        with patch.object(hook, "_git_output", return_value=""):
            with patch.object(hook, "_path_reference", return_value="deadbeef"):
                self.assertEqual(
                    hook.build_metadata_prefix(data),
                    "[repo:demo ref:deadbeef]",
                )

    def test_formats_clipboard_prompt_with_metadata(self) -> None:
        prompt = _long_prompt()

        with patch.object(
            hook,
            "build_metadata_prefix",
            return_value="[repo:demo thread:0199a213]",
        ):
            self.assertEqual(
                hook.format_clipboard_prompt(prompt, {}),
                f"[repo:demo thread:0199a213]\n{prompt}",
            )

    def test_format_skips_short_prompt_before_metadata(self) -> None:
        with patch.object(hook, "build_metadata_prefix") as mock_prefix:
            self.assertEqual(
                hook.format_clipboard_prompt("x" * (hook.MIN_PROMPT_CHARS - 1), {}),
                "",
            )
            mock_prefix.assert_not_called()

    def test_format_skips_prompt_short_after_stripping_code_and_paste(self) -> None:
        prompt = (
            "review this\n"
            "[Pasted Content 123,456 chars]\n"
            "```python\n"
            f"{'x' * hook.MIN_PROMPT_CHARS}\n"
            "```"
        )

        with patch.object(hook, "build_metadata_prefix") as mock_prefix:
            self.assertEqual(hook.format_clipboard_prompt(prompt, {}), "")
            mock_prefix.assert_not_called()

    def test_format_skips_metadata_when_prompt_is_empty(self) -> None:
        with patch.object(hook, "build_metadata_prefix") as mock_prefix:
            self.assertEqual(hook.format_clipboard_prompt("   \n", {}), "")
            mock_prefix.assert_not_called()

    def test_format_skips_non_human_source_metadata(self) -> None:
        prompt = _long_prompt()

        with patch.object(hook, "build_metadata_prefix") as mock_prefix:
            self.assertEqual(
                hook.format_clipboard_prompt(prompt, {"role": "assistant"}),
                "",
            )
            self.assertEqual(
                hook.format_clipboard_prompt(prompt, {"message": {"source": "subagent"}}),
                "",
            )
            mock_prefix.assert_not_called()

    def test_format_keeps_codex_product_source_metadata(self) -> None:
        prompt = _long_prompt()

        with patch.object(hook, "build_metadata_prefix", return_value=""):
            self.assertEqual(
                hook.format_clipboard_prompt(prompt, {"source": "codex"}),
                prompt,
            )

    def test_format_skips_subagent_notification(self) -> None:
        prompt = (
            "<subagent_notification>\n"
            '{"status":{"completed":"'
            + ("x" * hook.MIN_PROMPT_CHARS)
            + '"}}\n'
            "</subagent_notification>"
        )

        with patch.object(hook, "build_metadata_prefix") as mock_prefix:
            self.assertEqual(hook.format_clipboard_prompt(prompt, {}), "")
            mock_prefix.assert_not_called()

    def test_format_skips_subagent_control_prompt(self) -> None:
        prompt = (
            "Please stop the broad hunt and return your best current "
            "UTXO-style findings now. If no BTC/LTC/DASH/DOGE/BCH/ZEC "
            "addresses meet the high-activity + strong ShapeShift-control "
            "bar, say that clearly with the best sources checked. No edits."
        )

        with patch.object(hook, "build_metadata_prefix") as mock_prefix:
            self.assertEqual(hook.format_clipboard_prompt(prompt, {}), "")
            mock_prefix.assert_not_called()


class TestMain(unittest.TestCase):
    def _run_main(self, raw: str) -> Any:
        with patch.object(
            hook,
            "build_metadata_prefix",
            return_value="[repo:demo thread:0199a213]",
        ):
            with patch.object(hook.sys, "stdin", StringIO(raw)):
                with self.assertRaises(SystemExit) as exc_info:
                    hook.main()

        return exc_info.exception.code

    @patch.object(hook.subprocess, "run")
    def test_copies_sanitized_prompt(self, mock_run: MagicMock) -> None:
        prompt = _long_prompt()
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        self.assertEqual(self._run_main(json.dumps({"prompt": prompt})), 0)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0], [hook.PBCOPY])
        self.assertEqual(
            mock_run.call_args.kwargs["input"],
            f"[repo:demo thread:0199a213]\n{prompt}",
        )

    @patch.object(hook.subprocess, "run")
    def test_writes_nothing_to_stdout(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        stdout = StringIO()

        with redirect_stdout(stdout):
            self.assertEqual(self._run_main(json.dumps({"prompt": _long_prompt()})), 0)

        self.assertEqual(stdout.getvalue(), "")

    @patch.object(hook.subprocess, "run")
    def test_skips_pbcopy_when_prompt_is_short(self, mock_run: MagicMock) -> None:
        prompt = "x" * (hook.MIN_PROMPT_CHARS - 1)

        self.assertEqual(self._run_main(json.dumps({"prompt": prompt})), 0)
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_skips_pbcopy_when_prompt_is_short_after_sanitize(
        self, mock_run: MagicMock
    ) -> None:
        prompt = (
            "review this\n"
            "[Pasted Content 123,456 chars]\n"
            "```python\n"
            f"{'x' * hook.MIN_PROMPT_CHARS}\n"
            "```"
        )

        self.assertEqual(self._run_main(json.dumps({"prompt": prompt})), 0)
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_skips_pbcopy_when_empty(self, mock_run: MagicMock) -> None:
        self.assertEqual(self._run_main(json.dumps({"prompt": "   "})), 0)
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_skips_pbcopy_when_prompt_missing(self, mock_run: MagicMock) -> None:
        self.assertEqual(self._run_main(json.dumps({"session_id": "abc"})), 0)
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_skips_pbcopy_when_prompt_is_not_string(self, mock_run: MagicMock) -> None:
        self.assertEqual(self._run_main(json.dumps({"prompt": 123})), 0)
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_skips_pbcopy_for_non_human_source(self, mock_run: MagicMock) -> None:
        self.assertEqual(
            self._run_main(json.dumps({"prompt": _long_prompt(), "actor": "agent"})),
            0,
        )
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_skips_pbcopy_for_subagent_control_prompt(
        self, mock_run: MagicMock
    ) -> None:
        prompt = (
            "Please stop the broad hunt and return your best current "
            "UTXO-style findings now. If no BTC/LTC/DASH/DOGE/BCH/ZEC "
            "addresses meet the high-activity + strong ShapeShift-control "
            "bar, say that clearly with the best sources checked. No edits."
        )

        self.assertEqual(self._run_main(json.dumps({"prompt": prompt})), 0)
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_metadata_failure_copies_sanitized_prompt(
        self, mock_run: MagicMock
    ) -> None:
        prompt = _long_prompt()
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        stderr = StringIO()

        with patch.object(
            hook,
            "build_metadata_prefix",
            side_effect=RuntimeError("boom"),
        ):
            with patch.object(
                hook.sys,
                "stdin",
                StringIO(json.dumps({"prompt": prompt})),
            ):
                with redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as exc_info:
                        hook.main()

        self.assertEqual(exc_info.exception.code, 0)
        self.assertEqual(mock_run.call_args.kwargs["input"], prompt)
        self.assertIn("Warning: metadata prefix failed", stderr.getvalue())

    @patch.object(hook.subprocess, "run")
    def test_exits_on_invalid_json(self, mock_run: MagicMock) -> None:
        self.assertEqual(self._run_main("not valid json{"), 0)
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_exits_on_non_object_json(self, mock_run: MagicMock) -> None:
        self.assertEqual(self._run_main("123"), 0)
        mock_run.assert_not_called()

    @patch.object(hook.subprocess, "run")
    def test_handles_pbcopy_launch_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = OSError("pbcopy not found")
        stderr = StringIO()

        with redirect_stderr(stderr):
            self.assertEqual(self._run_main(json.dumps({"prompt": _long_prompt()})), 0)

        self.assertIn("Warning: pbcopy failed", stderr.getvalue())

    @patch.object(hook.subprocess, "run")
    def test_handles_pbcopy_nonzero_exit(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="nope")
        stderr = StringIO()

        with redirect_stderr(stderr):
            self.assertEqual(self._run_main(json.dumps({"prompt": _long_prompt()})), 0)

        self.assertIn("Warning: pbcopy exited 1: nope", stderr.getvalue())

    def test_debug_appends_raw_stdin(self) -> None:
        raw = json.dumps({"prompt": "debug me"})

        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "copy_prompt_to_clipboard.py"

            with patch.dict(hook.os.environ, {"CODEX_CLIP_DEBUG": "1"}):
                with patch.object(hook, "__file__", str(script_path)):
                    hook._maybe_debug(raw)

            debug_path = Path(temp_dir) / ".debug.jsonl"
            self.assertEqual(debug_path.read_text(encoding="utf-8"), raw + "\n")


if __name__ == "__main__":
    unittest.main()
