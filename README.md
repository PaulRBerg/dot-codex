# Codex Config

Personal `~/.codex` configuration and workflows for the Codex CLI.

## Layout

- `AGENTS.md`: canonical agent instructions (generated)
- `AGENTS_symlink.md`: symlinked source for instructions
- `context/AGENTS_EXTRA.md`: appended context injected into `AGENTS.md`
- `config.toml`: local runtime configuration (not checked in)
- `config-demo.toml`: safe template for `config.toml`
- `hooks.json`: tracked global Codex hooks
- `hooks/`: hook scripts and tests
- `justfile`: automation for regenerating context
- `helpers/flatten.py`: helper for flattening agent context
- `rules/`: grouped Codex command approval rules
- `prompts/`: prompt snippets
- `sessions/`: saved sessions
- `history.jsonl`: local run history

## Usage

```bash
just build
just test
```

Regenerates `AGENTS.md` by flattening `AGENTS_symlink.md` and appending `context/AGENTS_EXTRA.md`.
Runs hook unit tests with stdlib `unittest`.

## Hooks

`hooks.json` registers global Codex CLI hooks. Codex loads it from `~/.codex/hooks.json`.

Active hooks:

- `hooks/UserPromptSubmit/copy_prompt_to_clipboard.py`: copies each submitted prompt to the macOS clipboard via
  `/usr/bin/pbcopy` so Raycast clipboard history keeps a searchable prompt log.

The clipboard hook sanitizes noisy prompt content before copying:

- A compact metadata prefix such as `[repo:dot-codex thread:0199a213]` is prepended for provenance.
- Claude/Codex paste and image markers are normalized to `Pasted`.
- Fenced code blocks are collapsed to `[code]`, including unterminated fences.
- Long lines and over-cap prompts are bounded with `[Pasted]`.
- Blank lines are squeezed; empty sanitized prompts skip `pbcopy`.

The hook writes nothing to stdout. Warnings go to stderr and all failures exit 0 so prompt submission continues.

Set `CODEX_CLIP_DEBUG=1` to append raw hook stdin to `hooks/UserPromptSubmit/.debug.jsonl`.

After adding or changing a non-managed hook, open `/hooks` in Codex CLI to review and trust the hook definition.

## Setup

```bash
cp config-demo.toml config.toml
```

Edit `config.toml` for your local environment.

## Related

- https://github.com/PaulRBerg/dot-claude
- https://github.com/PaulRBerg/dot-gemini
