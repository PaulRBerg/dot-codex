#!/usr/bin/env -S uv run python
"""Integration tests for staged-snapshot pre-commit validation."""

from __future__ import annotations

import json
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
TRUST_CHECK_SOURCE = REPOSITORY_ROOT / "hooks" / "PreCommit" / "check_codex_hook_trust.py"


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
        self.active_hook_key = f"{self.repo}/hooks.json:user_prompt_submit:0:0"
        self.baseline_config = self._trust_config([self.active_hook_key])
        self._write("docs/note.md", "# Baseline\n")
        self._write("docs/name with spaces.md", "# Baseline spaces\n")
        self._write("config.toml", self.baseline_config)
        self._write(
            "hooks.json",
            '{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"true"}]}]}}\n',
        )
        self._git("add", "docs", "config.toml", "hooks.json")
        self._git("commit", "-qm", "Initial commit")

        (self.repo / ".husky").mkdir()
        (self.repo / "helpers").mkdir()
        (self.repo / "hooks" / "PreCommit").mkdir(parents=True)
        shutil.copy2(HOOK_SOURCE, self.repo / ".husky" / "pre-commit")
        shutil.copy2(CLEANER_SOURCE, self.repo / "helpers" / "codex-temp-clean")
        shutil.copy2(TRUST_CHECK_SOURCE, self.repo / "hooks" / "PreCommit" / "check_codex_hook_trust.py")
        self._write(".prettierrc.yml", "{}\n")
        self._write(".prettierignore", "")
        self._write("taplo.toml", "")
        self._write_tool("just", JUST_STUB)
        self._write_tool("bunx", BUNX_STUB)
        self._write_tool("taplo", TAPLO_STUB)
        self._write_tool("uv", UV_STUB)
        self._write_tool("codex", CODEX_STUB)

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

    def test_stale_trust_in_alternate_index_fails_without_using_worktree_config(self) -> None:
        alternate_index = self._alternate_index()
        stale_key = f"{self.repo}/hooks.json:user_prompt_submit:0:1"
        self._write("config.toml", self._trust_config([self.active_hook_key, stale_key]))
        self._git("add", "config.toml", env={"GIT_INDEX_FILE": str(alternate_index)})
        alternate_index_before = alternate_index.read_bytes()
        default_index_before = (self.repo / ".git" / "index").read_bytes()
        self._write("config.toml", self.baseline_config)

        result = self._run_hook(GIT_INDEX_FILE=str(alternate_index))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(stale_key, result.stderr)
        self.assertEqual((self.repo / "config.toml").read_text(), self.baseline_config)
        self.assertEqual(alternate_index.read_bytes(), alternate_index_before)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), default_index_before)
        self._assert_no_snapshots_remain()

    def test_unstaged_stale_trust_does_not_contaminate_staged_hooks_snapshot(self) -> None:
        self._write("hooks.json", '{"hooks":{"UserPromptSubmit":[]}}\n')
        self._git("add", "hooks.json")
        staged_index_before = (self.repo / ".git" / "index").read_bytes()
        stale_key = f"{self.repo}/hooks.json:user_prompt_submit:0:1"
        worktree_config = self._trust_config([self.active_hook_key, stale_key])
        self._write("config.toml", worktree_config)

        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / "config.toml").read_text(), worktree_config)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), staged_index_before)
        self._assert_no_snapshots_remain()

    def test_project_trust_is_discovered_from_its_project_root(self) -> None:
        project_root = Path(self.temporary_directory.name) / "project"
        (project_root / ".codex").mkdir(parents=True)
        project_key = f"{project_root}/.codex/hooks.json:session_start:0:0"
        self._write("config.toml", self._trust_config([self.active_hook_key, project_key]))
        self._git("add", "config.toml")

        result = self._run_hook(CODEX_PROJECT_ROOT=str(project_root))

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_no_snapshots_remain()

    def test_explicit_codex_home_is_distinct_from_validation_checkout(self) -> None:
        codex_home = Path(self.temporary_directory.name) / "canonical-codex-home"
        codex_home.mkdir()
        active_key = f"{codex_home}/hooks.json:user_prompt_submit:0:0"
        self._write("config.toml", self._trust_config([active_key]))
        self._git("add", "config.toml")

        result = self._run_hook(CODEX_HOME=str(codex_home))

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_no_snapshots_remain()

    def test_enabled_only_hook_state_is_not_a_trust_provision(self) -> None:
        stale_key = f"{self.repo}/hooks.json:user_prompt_submit:0:1"
        self._write("config.toml", self._trust_config([self.active_hook_key], enabled_only=[stale_key]))
        self._git("add", "config.toml")

        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_no_snapshots_remain()

    def test_discovery_failures_are_fatal(self) -> None:
        for mode in ("error", "malformed"):
            with self.subTest(mode=mode):
                alternate_index = self._alternate_index()
                self._write("config.toml", self._trust_config([self.active_hook_key], trailer=f"# {mode}\n"))
                self._git("add", "config.toml", env={"GIT_INDEX_FILE": str(alternate_index)})
                self._write("config.toml", self.baseline_config)

                result = self._run_hook(GIT_INDEX_FILE=str(alternate_index), CODEX_RESPONSE_MODE=mode)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not validate staged Codex hook trust state", result.stderr)
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

    def _trust_config(
        self, trusted: list[str], *, enabled_only: list[str] | None = None, trailer: str = ""
    ) -> str:
        lines = ["[hooks.state]", ""]
        for key in trusted:
            lines.extend([f"[hooks.state.{json.dumps(key)}]", 'trusted_hash = "sha256:test"', ""])
        for key in enabled_only or []:
            lines.extend([f"[hooks.state.{json.dumps(key)}]", "enabled = false", ""])
        if trailer:
            lines.append(trailer.rstrip("\n"))
        return "\n".join(lines).rstrip() + "\n"

    def _assert_no_snapshots_remain(self) -> None:
        self.assertEqual(set(self.snapshot_root.glob("codex-precommit.*")), self.existing_snapshots)

    def _run_hook(self, **environment: str) -> subprocess.CompletedProcess[str]:
        env = os.environ | environment
        env["PATH"] = f"{self.tools}{os.pathsep}{env['PATH']}"
        env["CODEX_HOME"] = environment.get("CODEX_HOME", str(self.repo))
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

UV_STUB = """#!/bin/sh
if [ "$1" = run ] && [ "$2" = python ]; then
  shift 2
  exec python3 "$@"
fi
exit 2
"""

CODEX_STUB = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

if sys.argv[1:] != ["app-server"]:
    raise SystemExit(2)

snapshot_home = Path(os.environ["CODEX_HOME"]).resolve()
mode = os.environ.get("CODEX_RESPONSE_MODE", "ok")
project_root_text = os.environ.get("CODEX_PROJECT_ROOT")
project_root = Path(project_root_text).resolve() if project_root_text else None

for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
        continue
    if message.get("method") != "hooks/list":
        continue
    if mode == "malformed":
        print(json.dumps({"id": message["id"], "result": {"data": "bad"}}), flush=True)
        continue

    entries = []
    global_source = snapshot_home / "hooks.json"
    for cwd in message["params"]["cwds"]:
        hooks = [{
            "key": f"{global_source}:user_prompt_submit:0:0",
            "sourcePath": str(global_source),
        }]
        if project_root is not None and Path(cwd).resolve() == project_root:
            project_source = project_root / ".codex" / "hooks.json"
            hooks.append({
                "key": f"{project_source}:session_start:0:0",
                "sourcePath": str(project_source),
            })
        errors = [{"message": "broken", "path": str(global_source)}] if mode == "error" else []
        entries.append({"cwd": cwd, "hooks": hooks, "warnings": [], "errors": errors})
    print(json.dumps({"id": message["id"], "result": {"data": entries}}), flush=True)
"""


if __name__ == "__main__":
    unittest.main()
