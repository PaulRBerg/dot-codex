# Codex Config

Personal `~/.codex` configuration and workflows for the Codex CLI.

## Layout

- `AGENTS.md`: canonical agent instructions (generated)
- `AGENTS_symlink.md`: symlinked source for instructions
- `context/AGENTS_EXTRA.md`: appended context injected into `AGENTS.md`
- `config.toml`: tracked runtime configuration
- `hooks.json`: tracked global Codex hooks
- `hooks/`: hook scripts and tests
- `justfile`: automation for regenerating context
- `helpers/flatten.py`: helper for flattening agent context
- `helpers/codex-temp-clean`: guarded cleanup for agent-owned temporary directories
- `rules/`: grouped Codex command approval rules
- `prompts/`: prompt snippets
- `sessions/`: saved sessions
- `history.jsonl`: local run history

## Usage

```bash
just build
just test
```

Regenerates `AGENTS.md` by flattening `AGENTS_symlink.md` and appending `context/AGENTS_EXTRA.md`. Runs hook unit tests
with stdlib `unittest`.

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
  [`ai-coord`](https://github.com/PaulRBerg/ai-coord) ledger used by Claude Code.

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

`ai-coord trailer` prints commit attribution. The `msg`, `inbox`, and `note` commands provide bounded peer communication
and durable repository findings. Private state lives under `$XDG_STATE_HOME/ai-coord`, defaulting to
`~/.local/state/ai-coord`.

After adding or changing a non-managed hook, open `/hooks` in Codex CLI to review and trust the hook definition.

## Related

- https://github.com/PaulRBerg/dot-claude
- https://github.com/PaulRBerg/dot-gemini
- https://github.com/PaulRBerg/ai-coord
