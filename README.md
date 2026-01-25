# Codex Config

Personal `~/.codex` configuration and workflows for the Codex CLI.

## Layout

- `AGENTS.md`: canonical agent instructions (generated)
- `AGENTS_symlink.md`: symlinked source for instructions
- `context/AGENTS_EXTRA.md`: appended context injected into `AGENTS.md`
- `config.toml`: local runtime configuration (not checked in)
- `config-demo.toml`: safe template for `config.toml`
- `justfile`: automation for regenerating context
- `flatten.py`: helper for flattening agent context
- `prompts/`: prompt snippets
- `sessions/`: saved sessions
- `history.jsonl`: local run history

## Usage

```bash
just build
```

Regenerates `AGENTS.md` by flattening `AGENTS_symlink.md` and appending `context/AGENTS_EXTRA.md`.

## Setup

```bash
cp config-demo.toml config.toml
```

Edit `config.toml` for your local environment.

## Related

- https://github.com/PaulRBerg/dot-claude
- https://github.com/PaulRBerg/dot-gemini
