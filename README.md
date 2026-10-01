# Codex Config

Personal `~/.codex` configuration and workflows for the Codex CLI.

## Layout

- `AGENTS.md`: shared agent instructions synced from `~/.agents/AGENTS.md`
- `config.toml`: tracked runtime configuration
- `hooks.json`: tracked global Codex hooks
- `hooks/`: hook scripts and tests
- `justfile`: checks and hook tests
- `ruff.toml`: Ruff lint and format configuration for the hook Python
- `helpers/codex-temp-clean`: guarded cleanup for agent-owned temporary directories
- `rules/`: grouped Codex command approval rules
- `sessions/`: saved sessions
- `history.jsonl`: local run history

## Usage

```bash
just
just test
```

Lists available recipes and runs hook unit tests with stdlib `unittest`.

Edit global instructions in `~/.agents/AGENTS.md`. That repository's commit hook copies them unchanged into this
repository, commits the update, and pushes it; do not hand-edit `AGENTS.md` here.

## Computer use

Start terminal sessions with `codex` or the chezmoi-managed `c` alias. Both use `config.toml` without a profile,
allowing the CLI to use the shared background server. Native Computer Use is enabled through `features.computer_use`;
Chrome DevTools remains enabled.

`helpers/cua-repl` remains available for an explicitly configured `cua_repl` MCP server. It launches the supplied
runtime command in a separate session while preserving its standard streams and forwarding termination signals. This
prevents macOS terminal job control from suspending the runtime with `SIGTTOU` when Codex runs in a terminal. No
`cua_repl` MCP server is currently configured.

The launcher reads `CFBundleShortVersionString` from `/Applications/ChatGPT.app/Contents/Info.plist` on every launch and
sets `BROWSER_USE_CODEX_APP_VERSION`, overriding any inherited value. Start a fresh session after changing its launcher
or MCP configuration. Native interaction requires the service's normal macOS and application permissions.

## Temporary cleanup

The tracked `helpers/codex-temp-clean` executable is available as soon as this repository is cloned into `~/.codex`; it
requires no separate installation. Use it for recursive cleanup of uniquely named temporary fixtures:

```bash
fixture="$(mktemp -d "${TMPDIR:-/tmp}/codex-smoke.XXXXXX")"
~/.codex/helpers/codex-temp-clean "$fixture"
```

The helper validates all targets before deleting any of them. Each target must be an absolute, non-symlinked,
current-user-owned mode-`0700` directory named `codex-*` directly beneath `/tmp` or the macOS per-user temporary root.

## Hooks

`hooks.json` registers global Codex CLI hooks. Codex loads it from `~/.codex/hooks.json`.

Active hooks:

- `hooks/UserPromptSubmit/copy_prompt_to_clipboard.py`: copies each submitted prompt to the macOS clipboard via
  `/usr/bin/pbcopy` so Raycast clipboard history keeps a searchable prompt log.
- `ai-coord hook codex`: tracks Codex lifecycle, presence, work ownership, messages, and repository findings in the
  shared [`ai-coord`](https://github.com/PaulRBerg/agent-toolkit/tree/main/coord) ledger used by Claude Code.
- `hooks/PreToolUse/git_guard.py`: denies git commands that sweep other agents' shared-worktree work (bare `git stash`,
  `git add -A`/`.`, `git commit -a`, `git checkout .`, `git restore .`, `git reset --hard`, `git clean`, autostash) with
  options in any order, and force pushes outside the `git push [origin] <force-flag>` forms that `rules/git.rules`
  prompts on. Execpolicy prefix rules cannot match options after other arguments or anchor at the end of a command, so
  the rules keep only the prefix forms and leave `git stash list`/`show` usable.
- `ai-notify event codex`: records task context on `UserPromptSubmit` and sends desktop completion notifications on
  `Stop`. Codex disables native hooks for internal title generation. The Desktop-owned `notify` wrapper no longer
  forwards to ai-notify, preventing duplicate completion alerts.

The clipboard hook sanitizes noisy prompt content before copying:

- A compact metadata prefix such as `[repo:dot-codex thread:0199a213]` is prepended for provenance.
- Claude/Codex paste and image markers are normalized to `Pasted`.
- Fenced code blocks are collapsed to `[code]`, including unterminated fences.
- Long lines and over-cap prompts are bounded with `[Pasted]`.
- Blank lines are squeezed; empty sanitized prompts skip `pbcopy`.

The hook writes nothing to stdout. Warnings go to stderr and all failures exit 0 so prompt submission continues.

Set `CODEX_CLIP_DEBUG=1` to append raw hook stdin to `hooks/UserPromptSubmit/.debug.jsonl`.

Check the installation or report Codex and Claude Code sessions:

```bash
ai-coord check
ai-coord status
ai-coord status --all
ai-coord status --json
```

Acquire literal repository paths before editing, wait for queued work, then release the scope when complete:

```bash
ai-coord start "task label" "src/owned-path"
ai-coord wait
ai-coord done
```

`ai-coord trailer` prints commit attribution. The `msg`, `inbox`, and `finding` commands provide bounded peer
communication and durable repository findings. Private state lives under `$XDG_STATE_HOME/ai-coord`, defaulting to
`~/.local/state/ai-coord`.

After adding or changing a non-managed hook, open `/hooks` in Codex CLI to review and trust the hook definition.

## Related

- https://github.com/PaulRBerg/dot-claude
- https://github.com/PaulRBerg/dot-gemini
- https://github.com/PaulRBerg/agent-toolkit/tree/main/coord
