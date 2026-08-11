#!/usr/bin/env -S uv run python
"""Integration tests for staged-snapshot pre-commit validation."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).parents[2]
HOOK_SOURCE = REPOSITORY_ROOT / ".husky" / "pre-commit"
CLEANER_SOURCE = REPOSITORY_ROOT / "helpers" / "codex-temp-clean"


class PreCommitHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo = Path(self.temporary_directory.name) / "repo"
        self.repo.mkdir()
        self.tools = self.repo / "tools"
        self.tools.mkdir()
        self.tool_log = self.repo / "tool.log"
        self.snapshot_root = Path(tempfile.gettempdir())
        self.existing_snapshots = set(self.snapshot_root.glob("codex-precommit.*"))

        self._git("init", "-q")
        self._git("config", "user.email", "hooks@example.test")
        self._git("config", "user.name", "Hook Test")
        self._write("docs/note.md", "# Baseline\n")
        self._write("docs/name with spaces.md", "# Baseline spaces\n")
        self._git("add", "docs")
        self._git("commit", "-qm", "Initial commit")

        (self.repo / ".husky").mkdir()
        (self.repo / "helpers").mkdir()
        shutil.copy2(HOOK_SOURCE, self.repo / ".husky" / "pre-commit")
        shutil.copy2(CLEANER_SOURCE, self.repo / "helpers" / "codex-temp-clean")
        self._write(".prettierrc.yml", "{}\n")
        self._write(".prettierignore", "")
        self._write("taplo.toml", "")
        self._write_tool("just", JUST_STUB)
        self._write_tool("bunx", BUNX_STUB)
        self._write_tool("taplo", TAPLO_STUB)

    def test_default_index_validates_staged_snapshot_and_preserves_shared_state(self) -> None:
        self._write("docs/note.md", "# Intended\n")
        self._git("add", "docs/note.md")
        index_before = (self.repo / ".git" / "index").read_bytes()
        worktree_contents = "# UNFORMATTED\n"
        self._write("docs/note.md", worktree_contents)

        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / "docs" / "note.md").read_text(), worktree_contents)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)
        tool_log = self.tool_log.read_text()
        self.assertIn("just-index:\n", tool_log)
        self.assertIn("bunx-arg:docs/note.md\n", tool_log)
        self._assert_no_snapshots_remain()

    def test_alternate_index_validates_snapshot_and_preserves_shared_state(self) -> None:
        self._write("shared-stage.txt", "Unrelated shared staging.\n")
        self._git("add", "shared-stage.txt")
        default_index_before = (self.repo / ".git" / "index").read_bytes()
        alternate_index = self._alternate_index()

        self._write("docs/note.md", "# Intended\n")
        self._write("docs/name with spaces.md", "# Intended spaces\n")
        self._git("add", "docs/note.md", "docs/name with spaces.md", env={"GIT_INDEX_FILE": str(alternate_index)})
        worktree_contents = "# Intended\nUnrelated worktree bytes.\n"
        self._write("docs/note.md", worktree_contents)

        result = self._run_hook(GIT_INDEX_FILE=str(alternate_index))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / "docs" / "note.md").read_text(), worktree_contents)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), default_index_before)
        self.assertEqual(self._git("diff", "--cached", "--name-only").stdout, "shared-stage.txt\n")
        tool_log = self.tool_log.read_text()
        self.assertIn(f"just-index:{alternate_index}\n", tool_log)
        self.assertIn("bunx-arg:docs/note.md\n", tool_log)
        self.assertIn("bunx-arg:docs/name with spaces.md\n", tool_log)
        self._assert_no_snapshots_remain()

    def test_unformatted_markdown_snapshot_fails_without_changing_worktree(self) -> None:
        alternate_index, worktree_contents = self._prepare_unformatted_snapshot("docs/unformatted.md", "# UNFORMATTED\n")

        result = self._run_hook(GIT_INDEX_FILE=str(alternate_index))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.repo / "docs" / "unformatted.md").read_text(), worktree_contents)
        self.assertIn("bunx-arg:docs/unformatted.md\n", self.tool_log.read_text())
        self._assert_no_snapshots_remain()

    def test_unformatted_toml_snapshot_fails_without_changing_worktree(self) -> None:
        alternate_index, worktree_contents = self._prepare_unformatted_snapshot(
            "settings.toml", "title = \"UNFORMATTED\"\n"
        )

        result = self._run_hook(GIT_INDEX_FILE=str(alternate_index))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.repo / "settings.toml").read_text(), worktree_contents)
        self.assertIn("taplo-arg:settings.toml\n", self.tool_log.read_text())
        self._assert_no_snapshots_remain()

    def test_gitleaks_failure_is_fatal_for_an_alternate_index(self) -> None:
        alternate_index = self._alternate_index()
        self._write("docs/note.md", "# Intended\n")
        self._git("add", "docs/note.md", env={"GIT_INDEX_FILE": str(alternate_index)})
        worktree_contents = "# Intended\nUnrelated worktree bytes.\n"
        self._write("docs/note.md", worktree_contents)

        result = self._run_hook(GIT_INDEX_FILE=str(alternate_index), GITLEAKS_EXIT="1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.repo / "docs" / "note.md").read_text(), worktree_contents)
        tool_log = self.tool_log.read_text()
        self.assertIn(f"just-index:{alternate_index}\n", tool_log)
        self.assertNotIn("bunx-arg:", tool_log)
        self._assert_no_snapshots_remain()

    def test_fully_staged_file_uses_snapshot_validation(self) -> None:
        self._write("docs/note.md", "# Fully staged\n")
        self._git("add", "docs/note.md")

        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        tool_log = self.tool_log.read_text()
        self.assertIn("just-index:\n", tool_log)
        self.assertIn("bunx-arg:docs/note.md\n", tool_log)
        self._assert_no_snapshots_remain()

    def test_deletion_only_commit_runs_gitleaks_without_formatters(self) -> None:
        (self.repo / "docs" / "note.md").unlink()
        self._git("add", "docs/note.md")

        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tool_log.read_text(), "just-index:\n")
        self._assert_no_snapshots_remain()

    def _alternate_index(self) -> Path:
        alternate_index = self.repo / "alternate.index"
        shutil.copy2(self.repo / ".git" / "index", alternate_index)
        return alternate_index

    def _prepare_unformatted_snapshot(self, filename: str, contents: str) -> tuple[Path, str]:
        alternate_index = self._alternate_index()
        self._write(filename, contents)
        self._git("add", filename, env={"GIT_INDEX_FILE": str(alternate_index)})
        worktree_contents = f"{contents}Unrelated worktree bytes.\n"
        self._write(filename, worktree_contents)
        return alternate_index, worktree_contents

    def _assert_no_snapshots_remain(self) -> None:
        self.assertEqual(set(self.snapshot_root.glob("codex-precommit.*")), self.existing_snapshots)

    def _run_hook(self, **environment: str) -> subprocess.CompletedProcess[str]:
        env = os.environ | environment
        env["PATH"] = f"{self.tools}{os.pathsep}{env['PATH']}"
        env["TOOL_LOG"] = str(self.tool_log)
        env["TMPDIR"] = str(self.snapshot_root)
        return subprocess.run(
            ["sh", ".husky/pre-commit"],
            cwd=self.repo,
            env=env,
            check=False,
            text=True,
            capture_output=True,
        )

    def _git(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        command_env = os.environ | (env or {})
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repo,
            env=command_env,
            check=True,
            text=True,
            capture_output=True,
        )

    def _write(self, filename: str, contents: str) -> None:
        destination = self.repo / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents)

    def _write_tool(self, name: str, contents: str) -> None:
        destination = self.tools / name
        destination.write_text(contents)
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR)


JUST_STUB = """#!/bin/sh
printf 'just-index:%s\\n' "${GIT_INDEX_FILE-}" >> "$TOOL_LOG"
exit "${GITLEAKS_EXIT:-0}"
"""

BUNX_STUB = """#!/bin/sh
for argument in "$@"; do
  printf 'bunx-arg:%s\\n' "$argument" >> "$TOOL_LOG"
  case "$argument" in
    *.md|*.json|*.jsonc|*.yaml|*.yml)
      if grep -q UNFORMATTED "$argument"; then
        exit 1
      fi
      ;;
  esac
done
"""

TAPLO_STUB = """#!/bin/sh
for argument in "$@"; do
  printf 'taplo-arg:%s\\n' "$argument" >> "$TOOL_LOG"
  case "$argument" in
    *.toml)
      if grep -q UNFORMATTED "$argument"; then
        exit 1
      fi
      ;;
  esac
done
"""


if __name__ == "__main__":
    unittest.main()
