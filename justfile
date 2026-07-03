# See https://just.systems/man/en/settings.html
set allow-duplicate-recipes
set allow-duplicate-variables
set shell := ["bash", "-euo", "pipefail", "-c"]
set unstable

# ---------------------------------------------------------------------------- #
#                                 DEPENDENCIES                                 #
# ---------------------------------------------------------------------------- #

uv := require("uv")
gitleaks := require("gitleaks")

# ---------------------------------------------------------------------------- #
#                                   COMMANDS                                   #
# ---------------------------------------------------------------------------- #

@default:
    just build

# Check git history for leaked secrets; pass a git revision/range like `origin/main..HEAD`
[group("checks")]
[positional-arguments]
@gitleaks-check range="HEAD":
    git rev-list --max-count=1 "$1" >/dev/null
    {{ gitleaks }} git --config .gitleaks.toml --redact --no-banner --log-opts "$1" .
alias glc := gitleaks-check

# Check staged changes for leaked secrets
[group("checks")]
@gitleaks-staged:
    {{ gitleaks }} git --config .gitleaks.toml --redact --no-banner --staged .
alias gls := gitleaks-staged

# Install tracked Git hooks for this repo.
@hooks-install:
    git config core.hooksPath .githooks

# Run the flatten script; accepts a `files` arg mirroring flatten.py
[private]
@flatten files="AGENTS_symlink.md" *args:
    {{ uv }} run python ~/.codex/helpers/flatten.py {{ files }} {{ args }}

# Build AGENTS.md by flattening and appending extra context.
@build:
    just flatten
    cat context/AGENTS_EXTRA.md >> AGENTS.md
alias b := build

# Run hook unit tests.
@test-hooks:
    {{ uv }} run python -m unittest hooks/UserPromptSubmit/copy_prompt_to_clipboard_test.py

# Run all tests.
@test: test-hooks
