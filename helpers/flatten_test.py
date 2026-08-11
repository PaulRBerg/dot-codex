#!/usr/bin/env -S uv run python
"""Unit tests for the Markdown reference flattener."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import flatten


class FlattenCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source.md"
        self.reference = self.root / "reference.md"
        self.source.write_text("Before\n@reference.md\nAfter\n", encoding="utf-8")
        self.reference.write_text("Included\n", encoding="utf-8")

    def test_default_invocation_preserves_agents_output_path(self) -> None:
        default_source = self.root / "AGENTS_symlink.md"
        default_source.write_text("Before\n@reference.md\nAfter\n", encoding="utf-8")

        with chdir(self.root):
            self.assertEqual(flatten.main([]), 0)

        output = self.root / "AGENTS.md"
        self.assertEqual(output.read_text(encoding="utf-8"), "Before\nIncluded\nAfter\n")
        self.assertEqual(default_source.read_text(encoding="utf-8"), "Before\n@reference.md\nAfter\n")

    def test_explicit_output_writes_single_input_to_requested_path(self) -> None:
        output = self.root / "rendered" / "context.md"
        output.parent.mkdir()

        self.assertEqual(flatten.main(["--output", str(output), str(self.source)]), 0)

        self.assertEqual(output.read_text(encoding="utf-8"), "Before\nIncluded\nAfter\n")
        self.assertFalse((self.root / "source_flattened.md").exists())

    def test_dry_run_prints_without_writing(self) -> None:
        stdout = StringIO()

        with redirect_stdout(stdout):
            self.assertEqual(flatten.main(["--dry-run", str(self.source)]), 0)

        self.assertEqual(stdout.getvalue(), "Before\nIncluded\nAfter\n")
        self.assertFalse((self.root / "source_flattened.md").exists())

    def test_output_and_dry_run_are_mutually_exclusive(self) -> None:
        self.assert_cli_error("--dry-run", "--output", str(self.root / "output.md"), str(self.source))

    def test_output_requires_exactly_one_input(self) -> None:
        other = self.root / "other.md"
        other.write_text("Other\n", encoding="utf-8")

        self.assert_cli_error("--output", str(self.root / "output.md"), str(self.source), str(other))

    def test_output_requires_an_explicit_input(self) -> None:
        self.assert_cli_error("--output", str(self.root / "output.md"))

    def test_explicit_output_cannot_overwrite_input(self) -> None:
        original = self.source.read_text(encoding="utf-8")

        self.assert_cli_error("--output", str(self.source), str(self.source))

        self.assertEqual(self.source.read_text(encoding="utf-8"), original)

    def test_explicit_output_cannot_overwrite_input_through_symlink(self) -> None:
        output = self.root / "output.md"
        output.symlink_to(self.source)
        original = self.source.read_text(encoding="utf-8")

        self.assert_cli_error("--output", str(output), str(self.source))

        self.assertEqual(self.source.read_text(encoding="utf-8"), original)

    def test_explicit_output_cannot_overwrite_input_through_hard_link(self) -> None:
        output = self.root / "output.md"
        os.link(self.source, output)
        original = self.source.read_text(encoding="utf-8")

        self.assert_cli_error("--output", str(output), str(self.source))

        self.assertEqual(self.source.read_text(encoding="utf-8"), original)

    def assert_cli_error(self, *arguments: str) -> None:
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                flatten.main(list(arguments))
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
