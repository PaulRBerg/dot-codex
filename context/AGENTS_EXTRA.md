
## Speed Traps

- Verify paths and cwd before commands that assume a location: use `test -e`, `rg --files`, or `fd` instead of guessing.
- For patch-compatible TSV diffs, use `git diff --no-ext-diff --no-textconv -- <paths>`. Never pipe daff-rendered TSV
  diffs into `git apply`.
- Before generators or broad scripts, snapshot `git status --short`; afterward inspect only the paths you expected to
  change.
- Cap private financial CSV/TSV output. Summarize counts and file refs unless raw rows were explicitly requested.
- Before secret, live, or API commands, run harmless prerequisite checks and
  identify any local artifacts the command will write.

## Codex

- `~/.codex/AGENTS.md` is generated from `AGENTS_symlink.md` plus `context/AGENTS_EXTRA.md`. Edit those sources and run `just build`; do not hand-edit the generated file.
