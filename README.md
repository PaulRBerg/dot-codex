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
- `hooks/AgentSessionStatus/agent_session_status.py`: maintains a privacy-minimal registry for in-flight Codex turns and
  combines it with Claude Code's native session inventory.

The clipboard hook sanitizes noisy prompt content before copying:

- A compact metadata prefix such as `[repo:dot-codex thread:0199a213]` is prepended for provenance.
- Claude/Codex paste and image markers are normalized to `Pasted`.
- Fenced code blocks are collapsed to `[code]`, including unterminated fences.
- Long lines and over-cap prompts are bounded with `[Pasted]`.
- Blank lines are squeezed; empty sanitized prompts skip `pbcopy`.

The hook writes nothing to stdout. Warnings go to stderr and all failures exit 0 so prompt submission continues.

Set `CODEX_CLIP_DEBUG=1` to append raw hook stdin to `hooks/UserPromptSubmit/.debug.jsonl`.

Report executing Codex and Claude Code sessions machine-wide:

```bash
~/.codex/hooks/AgentSessionStatus/agent_session_status.py status
~/.codex/hooks/AgentSessionStatus/agent_session_status.py status --json
```

The status command includes Codex turns between `UserPromptSubmit` and `Stop`/`SessionEnd`, plus Claude sessions that
are working, waiting for input, or idle. It excludes completed sessions. Exit `0` means both providers were available,
exit `2` means the output has partial provider coverage, and exit `64` means invalid command usage.

Identify the calling session, attach a task label, or manage a repository-scoped note:

```bash
~/.codex/hooks/AgentSessionStatus/agent_session_status.py identity
~/.codex/hooks/AgentSessionStatus/agent_session_status.py claim "task label"
~/.codex/hooks/AgentSessionStatus/agent_session_status.py note "observation"
~/.codex/hooks/AgentSessionStatus/agent_session_status.py note --done <note-id>
```

Session records contain only session and turn IDs, cwd, state, timestamps, and a process-lifetime identity. Claim
sidecars add a task label and session attribution; note files contain repository-scoped observation text with optional
session attribution and expire after seven days. All registry data is stored under
`~/.codex/.tmp/agent-session-status/` with private permissions.

After adding or changing a non-managed hook, open `/hooks` in Codex CLI to review and trust the hook definition.

## Related

- https://github.com/PaulRBerg/dot-claude
- https://github.com/PaulRBerg/dot-gemini
