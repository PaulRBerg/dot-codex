# Codex Config

Personal `~/.codex` configuration and workflows for the Codex CLI.

## Layout

- `AGENTS.md`: shared agent instructions synced from `~/.agents/AGENTS.md`
- `config.toml`: tracked runtime configuration
- `cli.config.toml`: CLI profile retaining a disabled detached Computer Use launcher configuration
- `hooks.json`: tracked global Codex hooks
- `hooks/`: hook scripts and tests
- `justfile`: checks and hook tests
- `helpers/codex-temp-clean`: guarded cleanup for agent-owned temporary directories
- `rules/`: grouped Codex command approval rules
- `prompts/`: prompt snippets
- `sessions/`: saved sessions
- `history.jsonl`: local run history

## Usage

```bash
just
just test
```

Lists available recipes and runs hook unit tests with stdlib `unittest`.

Edit global instructions in `~/.agents/AGENTS.md`. That repository's commit hook copies them unchanged into this
repository and commits the update; do not hand-edit `AGENTS.md` here.

## Computer use

Start terminal sessions with `codex --profile cli` (the chezmoi-managed `c` alias). Native Computer Use is disabled in
this profile. `cli.config.toml` retains the complete `cua_repl` definition so it can be explicitly re-enabled while
keeping its launcher and timeout outside Desktop's shared configuration rewrites. Chrome DevTools remains enabled.

When native Computer Use is explicitly re-enabled, it uses the runtime bundled with `/Applications/ChatGPT.app` and the
installed `~/.codex/computer-use/Codex Computer Use.app` service. `helpers/cua-repl` launches the MCP runtime in a
separate session while preserving its standard streams and forwarding termination signals. This prevents macOS terminal
job control from suspending the runtime with `SIGTTOU` when Codex runs in a terminal. The same launcher works without a
controlling terminal in the desktop app.

When re-enabled, the launcher reads `CFBundleShortVersionString` from `/Applications/ChatGPT.app/Contents/Info.plist` on
every launch and sets `BROWSER_USE_CODEX_APP_VERSION`, overriding any inherited value. App version updates require no
edits to `cli.config.toml`.

Start a fresh `codex --profile cli` session after changing this launcher or its MCP configuration. Confirm the effective
disabled state with `codex --profile cli mcp get cua_repl --json`. Native interaction is appropriate only after explicit
re-enablement and still requires the service's normal macOS and application permissions. MCP calls have a 45-second
timeout so a failed connection does not wait for several minutes. After Desktop runtime upgrades, compare the profile's
runtime paths and environment with the current bundled `unified-computer-use` manifest while preserving the detached
launcher and timeout.

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
- `ai-coord hook codex`: tracks Codex lifecycle, presence, work ownership, messages, and repository notes in the shared
  [`ai-coord`](https://github.com/PaulRBerg/agent-toolkit/tree/main/coord) ledger used by Claude Code.

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
