#!/usr/bin/env -S uv run python
"""Integration tests for shared-worktree-safe pre-commit validation."""

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
        self.bun_attempts = self.repo / "bun-attempts"
        self.snapshot_root = Path(tempfile.gettempdir())
        self.existing_snapshots = set(self.snapshot_root.glob("codex-precommit.*"))

        self._git("init", "-q")
        self._git("config", "user.email", "hooks@example.test")
        self._git("config", "user.name", "Hook Test")
        self.active_hook_key = f"{self.repo}/hooks.json:user_prompt_submit:0:0"
        self.baseline_config = self._trust_config([self.active_hook_key])
        self._write("docs/note.md", "# Baseline\n")
        self._write("config.toml", self.baseline_config)
        self._write(
            "hooks.json",
            '{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"true"}]}]}}\n',
        )
        self._git("add", "docs", "config.toml", "hooks.json")
        self._git("commit", "-qm", "Initial commit")

        (self.repo / ".husky").mkdir()
        (self.repo / "hooks" / "PreCommit").mkdir(parents=True)
        shutil.copy2(HOOK_SOURCE, self.repo / ".husky" / "pre-commit")
        shutil.copy2(TRUST_CHECK_SOURCE, self.repo / "hooks" / "PreCommit" / "check_codex_hook_trust.py")
        self._write_tool("bun", BUN_STUB)
        self._write_tool("uv", UV_STUB)
        self._write_tool("codex", CODEX_STUB)

    def test_empty_index_exits_without_running_lint_staged(self) -> None:
        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.tool_log.exists())

    def test_default_index_partial_stage_is_rejected(self) -> None:
        self._write("docs/note.md", "# Intended\n")
        self._git("add", "docs/note.md")
        self._write("docs/note.md", "# Intended\nUnrelated worktree bytes.\n")

        result = self._run_hook()

        self.assertEqual(result.returncode, 1)
        self.assertIn("error: partially staged files are unsafe in the shared worktree:", result.stderr)
        self.assertIn("docs/note.md", result.stderr)
        self.assertFalse(self.tool_log.exists())

    def test_alternate_index_partial_stage_is_rejected_without_changing_default_index(self) -> None:
        self._write("shared-stage.txt", "Unrelated shared staging.\n")
        self._git("add", "shared-stage.txt")
        default_index_before = (self.repo / ".git" / "index").read_bytes()
        alternate_index = self._alternate_index()
        self._write("docs/note.md", "# Intended\n")
        self._git("add", "docs/note.md", env={"GIT_INDEX_FILE": str(alternate_index)})
        self._write("docs/note.md", "# Intended\nUnrelated worktree bytes.\n")

        result = self._run_hook(GIT_INDEX_FILE=str(alternate_index))

        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/note.md", result.stderr)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), default_index_before)
        self.assertFalse(self.tool_log.exists())

    def test_fully_staged_file_runs_lint_staged(self) -> None:
        self._write("docs/note.md", "# Fully staged\n")
        self._git("add", "docs/note.md")

        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tool_log.read_text(), "bun-index:\nbun:run lint-staged\n")

    def test_fully_staged_alternate_index_leaves_default_index_untouched(self) -> None:
        self._write("shared-stage.txt", "Unrelated shared staging.\n")
        self._git("add", "shared-stage.txt")
        default_index_before = (self.repo / ".git" / "index").read_bytes()
        alternate_index = self._alternate_index()
        self._write("docs/note.md", "# Alternate index\n")
        self._git("add", "docs/note.md", env={"GIT_INDEX_FILE": str(alternate_index)})

        result = self._run_hook(GIT_INDEX_FILE=str(alternate_index))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), default_index_before)
        self.assertIn(f"bun-index:{alternate_index}\n", self.tool_log.read_text())

    def test_deletion_only_commit_runs_lint_staged(self) -> None:
        (self.repo / "docs" / "note.md").unlink()
        self._git("add", "docs/note.md")

        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bun:run lint-staged\n", self.tool_log.read_text())

    def test_non_transient_lint_staged_failure_is_not_retried(self) -> None:
        self._write("docs/note.md", "# Fully staged\n")
        self._git("add", "docs/note.md")

        result = self._run_hook(BUN_EXIT="7", BUN_ERROR="lint failure")

        self.assertEqual(result.returncode, 7)
        self.assertIn("lint failure", result.stdout)
        self.assertEqual(self.tool_log.read_text().count("bun:run lint-staged\n"), 1)

    def test_transient_lint_staged_failure_is_retried(self) -> None:
        self._write("docs/note.md", "# Fully staged\n")
        self._git("add", "docs/note.md")

        result = self._run_hook(BUN_MODE="transient")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tool_log.read_text().count("bun:run lint-staged\n"), 3)

    def test_stale_trust_in_alternate_index_uses_staged_snapshot(self) -> None:
        alternate_index = self._alternate_index()
        stale_key = f"{self.repo}/hooks.json:user_prompt_submit:0:1"
        self._write("config.toml", self._trust_config([self.active_hook_key, stale_key]))
        self._git("add", "config.toml", env={"GIT_INDEX_FILE": str(alternate_index)})
        alternate_index_before = alternate_index.read_bytes()
        default_index_before = (self.repo / ".git" / "index").read_bytes()
        self._write("config.toml", self.baseline_config)

        result = self._run_trust_check(GIT_INDEX_FILE=str(alternate_index))

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

    def test_deleted_hooks_rejects_stale_trust(self) -> None:
        (self.repo / "hooks.json").unlink()
        self._git("add", "hooks.json")

        result = self._run_hook()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(self.active_hook_key, result.stderr)
        self.assertFalse(self.tool_log.exists())
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

                result = self._run_trust_check(
                    GIT_INDEX_FILE=str(alternate_index),
                    CODEX_RESPONSE_MODE=mode,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not validate staged Codex hook trust state", result.stderr)
                self._assert_no_snapshots_remain()

    def _alternate_index(self) -> Path:
        alternate_index = self.repo / "alternate.index"
        shutil.copy2(self.repo / ".git" / "index", alternate_index)
        return alternate_index

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

    def _environment(self, **environment: str) -> dict[str, str]:
        env = os.environ | environment
        env["PATH"] = f"{self.tools}{os.pathsep}{env['PATH']}"
        env["CODEX_HOME"] = environment.get("CODEX_HOME", str(self.repo))
        env["TOOL_LOG"] = str(self.tool_log)
        env["BUN_ATTEMPTS_FILE"] = str(self.bun_attempts)
        env["TMPDIR"] = str(self.snapshot_root)
        return env

    def _run_hook(self, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", ".husky/pre-commit"],
            cwd=self.repo,
            env=self._environment(**environment),
            check=False,
            text=True,
            capture_output=True,
        )

    def _run_trust_check(self, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", "hooks/PreCommit/check_codex_hook_trust.py"],
            cwd=self.repo,
            env=self._environment(**environment),
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


class LintStagedConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo = Path(self.temporary_directory.name) / "repo"
        self.repo.mkdir()
        self.tools = self.repo / "tools"
        self.tools.mkdir()
        self.tool_log = self.repo / "tool.log"

        self._git("init", "-q")
        self._git("config", "user.email", "lint-staged@example.test")
        self._git("config", "user.name", "Lint Staged Test")
        self._write("docs/note.md", "# Baseline\n")
        self._write("settings.toml", 'title = "baseline"\n')
        self._git("add", "docs/note.md", "settings.toml")
        self._git("commit", "-qm", "Initial commit")
        self._write_tool("bunx", LINT_STAGED_BUNX_STUB)
        self._write_tool("just", LINT_STAGED_JUST_STUB)

    def test_formats_prettier_files_and_checks_toml_after_gitleaks(self) -> None:
        self._write("docs/note.md", "# Unformatted\n")
        self._write("settings.toml", 'title = "changed"\n')
        self._git("add", "docs/note.md", "settings.toml")
        env = os.environ | {
            "PATH": f"{self.tools}{os.pathsep}{os.environ['PATH']}",
            "TOOL_LOG": str(self.tool_log),
        }

        result = subprocess.run(
            [
                str(REPOSITORY_ROOT / "node_modules" / ".bin" / "lint-staged"),
                "--config",
                str(REPOSITORY_ROOT / ".lintstagedrc.mjs"),
                "--concurrent",
                "false",
                "--no-stash",
                "--no-hide-partially-staged",
            ],
            cwd=self.repo,
            env=env,
            check=False,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.tool_log.read_text()
        self.assertLess(log.index("just:gitleaks-staged"), log.index("bunx:"))
        self.assertLess(log.index("bunx:"), log.index("just:toml-format-check"))
        self.assertIn(str(self.repo / "docs" / "note.md"), log)
        self.assertIn(str(self.repo / "settings.toml"), log)
        self.assertEqual(self._git("show", ":docs/note.md").stdout, "# Formatted\n")

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repo,
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


BUN_STUB = """#!/bin/sh
printf 'bun-index:%s\\n' "${GIT_INDEX_FILE-}" >> "$TOOL_LOG"
printf 'bun:%s\\n' "$*" >> "$TOOL_LOG"
if [ "${BUN_MODE-}" = transient ]; then
  attempt=0
  if [ -f "$BUN_ATTEMPTS_FILE" ]; then
    attempt=$(sed -n '1p' "$BUN_ATTEMPTS_FILE")
  fi
  attempt=$((attempt + 1))
  printf '%s\\n' "$attempt" > "$BUN_ATTEMPTS_FILE"
  if [ "$attempt" -lt 3 ]; then
    printf 'Failed to get staged files!\\n' >&2
    exit 1
  fi
fi
if [ -n "${BUN_ERROR-}" ]; then
  printf '%s\\n' "$BUN_ERROR" >&2
fi
exit "${BUN_EXIT:-0}"
"""

UV_STUB = """#!/bin/sh
if [ "$1" = run ] && [ "$2" = python ]; then
  shift 2
  exec python3 "$@"
fi
exit 2
"""

LINT_STAGED_BUNX_STUB = """#!/bin/sh
printf 'bunx:%s\\n' "$*" >> "$TOOL_LOG"
for argument in "$@"; do
  case "$argument" in
    *.md) printf '# Formatted\\n' > "$argument" ;;
  esac
done
"""

LINT_STAGED_JUST_STUB = """#!/bin/sh
printf 'just:%s\\n' "$*" >> "$TOOL_LOG"
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
        hooks = []
        if global_source.exists():
            hooks.append({
                "key": f"{global_source}:user_prompt_submit:0:0",
                "sourcePath": str(global_source),
            })
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
