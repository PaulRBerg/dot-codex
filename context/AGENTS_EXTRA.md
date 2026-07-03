
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
- A plan's Codex Spark delegation section is only a candidate list. Spark usage counts only when implementation actually spawns a subagent with the Spark model.
- To spawn a Spark subagent, call the subagent tool with `model: "gpt-5.3-codex-spark"`; otherwise spawned agents inherit the parent model, which is usually full Codex.
- Use Spark subagents only for independent, low-risk, text-only, read-heavy, or surgical tasks where latency matters more than capability. Use full Codex for ambiguous design, broad edits, integration, browser/computer/image work, high-context debugging, and final verification.
- In Spark delegation sections, name the candidate role, scope, expected output, and whether implementation should spawn it with `model: "gpt-5.3-codex-spark"`.
- Keep delegation scoped to independent, low-risk subtasks; reserve main Codex for coordination, integration, and final verification.

## Codex

- `~/.codex/AGENTS.md` is generated from `AGENTS_symlink.md` plus `context/AGENTS_EXTRA.md`. Edit those sources and run `just build`; do not hand-edit the generated file.
- Ignore unrelated and/or unexpected modified files. Treat such changes as acceptable and proceed without asking how to handle them.
