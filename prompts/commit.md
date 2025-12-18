---
description: Create an atomic git commit with smart heuristic analysis
argument-hint: '[FILES=<paths>] [PR_TITLE="<title>"]'
---

You are in a git repo. Create a high-quality, mostly-atomic commit.

## Context (collect first)

Run and include the output of:

- `git branch --show-current`
- `git status --short --branch`
- `git diff --cached`
- `git diff`

Also echo the prompt args so they appear in the transcript:

- `echo "ARGS=$ARGUMENTS"`
- `echo "FILES=$FILES"`
- `echo "PR_TITLE=$PR_TITLE"`

## Staging rules

1) If `$FILES` is set:
   - Stage **exactly** those paths (space-separated list is fine).
   - Fail if any path does not exist.
   - Do not stage unrelated files.

2) Else if `$ARGUMENTS` contains `--all`:
   - Stage everything with `git add -A`.
   - If there are no changes to commit, stop with a clear error.

3) Else (default: atomic-by-session):
   - If there are already-staged changes that are not clearly from this Codex session, ask before modifying the index.
   - Unstage everything (`git reset`), then stage only the files modified in *this Codex session*.
   - If no files were modified in this session, stop with a clear error.

Log what was staged.

## Message rules

Prefer Conventional Commits:

- Subject line (≤50 chars): `type(scope): description` or `type: description`
- Imperative mood ("add" not "added"), lowercase, no trailing period
- Describe what the change does, not which files changed

Determine `type` from the staged diff:

- New functionality → `feat`
- Bug fix / error handling → `fix`
- Reorganization without behavior change → `refactor`
- Docs-only → `docs`
- Tests → `test`
- Build/CI → `ci`
- Dependencies → `chore(deps)`
- AI config (CLAUDE.md, AGENTS.md, `.claude/`, `.gemini/`, `.codex/`) → `ai`

Infer `scope` only when it’s obvious; otherwise omit it.

If `$PR_TITLE` is set:

- If it already matches Conventional Commits, use it as-is.
- Otherwise, treat it as the **description** to pair with the inferred `type(scope): ...`.

If `$ARGUMENTS` contains `--deep`:

- Do deeper semantic analysis, detect breaking changes, and include a wrapped (72 cols) body explaining **why**.
- If breaking: add `BREAKING CHANGE: ...` + migration notes.
- If you find issue references in the transcript, add `Closes #...`.

## Commit

- If `$ARGUMENTS` contains `--stack`: use `gt create -m "subject" -m "body"`.
- Else: use `git commit -m "subject"` (and add `-m "body"` only when a body is present).

Output:

- Commit hash + subject
- 2–3 bullets summarizing the key behavior-level changes

## Push (optional)

If `$ARGUMENTS` contains `--push`:

- If also `--stack`: run `gt stack submit`
- Else: run `git push origin`

On failure, show the error and the most likely fix (pull/rebase, set upstream, auth).
