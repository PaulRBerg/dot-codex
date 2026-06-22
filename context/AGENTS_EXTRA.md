
## Speed Traps

- Verify paths and cwd before commands that assume a location: use `test -e`, `rg --files`, or `fd` instead of guessing.
- Use single quotes or `rg -F` for shell-sensitive search patterns. Use `rg -P` only when the pattern needs lookaround or
  other PCRE features.
- For patch-compatible TSV diffs, use `git diff --no-ext-diff --no-textconv -- <paths>`. Never pipe daff-rendered TSV
  diffs into `git apply`.
- Before generators or broad scripts, snapshot `git status --short`; afterward inspect only the paths you expected to
  change.
- Cap private financial CSV/TSV output. Summarize counts and file refs unless raw rows were explicitly requested.
- Preflight secret, live, or API commands with harmless env checks, and state whether the command writes repo artifacts.

## Plan Mode

- When the user asks for a plan in Plan mode, and the task is relatively simple or decomposes into relatively simple subtasks, include a Codex Spark delegation section identifying work that can be handled by Codex Spark subagents to save Codex credits.
- Keep delegation scoped to independent, low-risk subtasks; reserve main Codex for coordination, integration, and final verification.

## Codex

- `~/.codex/AGENTS.md` is generated from `AGENTS_symlink.md` plus `context/AGENTS_EXTRA.md`. Edit those sources and run `just build`; do not hand-edit the generated file.
- Ignore unrelated and/or unexpected modified files. Treat such changes as acceptable and proceed without asking how to handle them.
