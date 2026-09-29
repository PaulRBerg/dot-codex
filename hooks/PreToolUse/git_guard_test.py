"""Tests for the git_guard PreToolUse hook."""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import git_guard  # noqa: E402

DENIED = [
    # Bare stash and its implicit or explicit push forms.
    "git stash",
    "git stash -u",
    "git stash -p",
    "git stash push -m wip",
    "git stash save wip",
    "git stash clear",
    "git stash -- src/app.ts",
    # Options after other arguments, which prefix rules cannot reach.
    "git commit -m msg -a",
    "git commit -qam msg",
    "git commit --amend --all --no-edit",
    "git commit -m msg .",
    "git add -v .",
    "git add --verbose -A",
    "git add -u",
    "git add :/",
    "git checkout HEAD -- .",
    "git restore --staged .",
    "git restore --source HEAD~1 .",
    "git reset HEAD~1 --hard",
    "git clean -n",
    "git pull --rebase --autostash",
    "git rebase main --autostash",
    "git -c rebase.autoStash=true pull --rebase",
    "git push origin main --force",
    "git push origin main -f",
    "git push --force-with-lease=main:abc123 origin main",
    "git push origin +main",
    "git push upstream --force",
    "git -C repo push --force origin main",
    # Global options, wrappers, compound commands, and nested shells.
    "git -C /tmp/repo stash",
    "git --no-pager -c color.ui=never stash",
    "GIT_TRACE=1 git stash",
    "env -u FOO git stash",
    "sudo -n git clean -fd",
    "timeout 10 git stash",
    "/usr/bin/git stash",
    "git status && git stash",
    "git status; git add .",
    "git fetch\ngit reset --hard origin/main",
    "echo $(git stash)",
    "echo `git stash`",
    "git stash 2>/dev/null",
    "git stash > /dev/null",
    "(cd repo && git add -A)",
    "bash -lc 'git stash'",
    "zsh -c \"git commit -m 'msg' -a\"",
    "bash --login -c 'git add .'",
]

ALLOWED = [
    "git stash list",
    "git stash show -p stash@{0}",
    "git stash pop",
    "git stash apply abc123",
    "git reflog show stash",
    "git commit -m msg",
    "git commit -m 'stage -a later' src/app.ts",
    "git commit -ma",
    "git commit -S -m msg",
    "git commit -F msg.txt src/app.ts",
    "git add src/app.ts",
    "git add -p src/app.ts",
    "git add -u src/",
    "git checkout main",
    "git checkout -- src/app.ts",
    "git restore --staged src/app.ts",
    "git reset --soft HEAD~1",
    "git pull --rebase --no-autostash",
    "git -c rebase.autoStash=false pull --rebase",
    "git push",
    "git push origin main",
    "git push --force-with-lease origin main",
    "git push origin --force",
    "git push -f",
    "git status --short",
    "echo 'git stash'",
    "rg -n 'git add -A' docs",
    "ai-commit commit abc -m 'Forbid git stash and git clean'",
    "bash scripts/check.sh",
    "",
]


class CommandDenialTest(unittest.TestCase):
    def test_denies_banned_git_forms(self) -> None:
        for command in DENIED:
            with self.subTest(command=command):
                self.assertIsNotNone(git_guard.command_denial(command))

    def test_allows_safe_git_forms(self) -> None:
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(git_guard.command_denial(command))

    def test_force_push_reason_names_the_prompted_form(self) -> None:
        reason = git_guard.command_denial("git push origin main --force")
        self.assertIn("git push --force-with-lease origin <branch>", reason)


class MainTest(unittest.TestCase):
    def run_hook(self, payload: object) -> str:
        stdout = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(stdout):
            self.assertEqual(git_guard.main(), 0)
        return stdout.getvalue()

    def test_emits_deny_decision(self) -> None:
        output = json.loads(self.run_hook({"tool_input": {"command": "git stash"}}))
        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("git stash list", decision["permissionDecisionReason"])

    def test_accepts_argv_commands(self) -> None:
        output = self.run_hook({"tool_input": {"command": ["bash", "-lc", "git add -A"]}})
        self.assertIn('"deny"', output)

    def test_allows_silently(self) -> None:
        self.assertEqual(self.run_hook({"tool_input": {"command": "git stash list"}}), "")

    def test_fails_open_on_malformed_input(self) -> None:
        self.assertEqual(self.run_hook({"tool_input": {}}), "")
        self.assertEqual(self.run_hook({"tool_input": {"command": "echo 'unterminated"}}), "")

    def test_hook_is_registered_for_bash(self) -> None:
        hooks = json.loads((Path(__file__).parents[2] / "hooks.json").read_text())
        commands = [
            hook["command"]
            for group in hooks["hooks"]["PreToolUse"]
            if group.get("matcher") == "Bash"
            for hook in group["hooks"]
        ]
        self.assertTrue(
            any(command.endswith("hooks/PreToolUse/git_guard.py") for command in commands)
        )


if __name__ == "__main__":
    unittest.main()
