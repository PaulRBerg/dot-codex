# See https://just.systems/man/en/settings.html
set allow-duplicate-recipes
set allow-duplicate-variables
set shell := ["bash", "-euo", "pipefail", "-c"]
set unstable

export RUST_LOG := "warn"

# ---------------------------------------------------------------------------- #
#                                 DEPENDENCIES                                 #
# ---------------------------------------------------------------------------- #

uv := require("uv")
gitleaks := require("gitleaks")
taplo := require("taplo")
prettier := "bunx --no-install prettier"
prettier_cache := ".cache/prettier/.prettier-cache"
prettier_globs := "\"**/*.{md,json,jsonc,yaml,yml}\""

# ---------------------------------------------------------------------------- #
#                                   COMMANDS                                   #
# ---------------------------------------------------------------------------- #

@default:
    just build

# Check documentation and configuration formatting.
[group("checks")]
@prettier-check +globs=prettier_globs:
    {{ prettier }} \
        --check \
        --cache \
        --cache-location {{ prettier_cache }} \
        --log-level warn \
        --no-error-on-unmatched-pattern \
        {{ globs }}
alias pc := prettier-check

# Format documentation and configuration.
[group("checks")]
@prettier-write +globs=prettier_globs:
    {{ prettier }} \
        --write \
        --cache \
        --cache-location {{ prettier_cache }} \
        --log-level warn \
        --no-error-on-unmatched-pattern \
        {{ globs }}
alias pw := prettier-write

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

# Check TOML formatting.
[group("checks")]
[positional-arguments]
@toml-format-check *files:
    {{ taplo }} format --check {{ files }}

# Format TOML files in place.
[group("checks")]
@toml-format-write:
    {{ taplo }} format

# Install Husky Git hooks for this checkout.
@hooks-install:
    bun run prepare

# Run staged-file checks.
[group("checks")]
@pre-commit:
    bash .husky/pre-commit
alias precommit := pre-commit

# Run the flatten script; accepts a `files` arg mirroring flatten.py
[private]
@flatten files="AGENTS_symlink.md" *args:
    {{ uv }} run python ~/.codex/helpers/flatten.py {{ files }} {{ args }}

# Build AGENTS.md from an optional source and append extra context.
@build source="AGENTS_symlink.md":
    just flatten {{ source }} --output AGENTS.md
    cat context/AGENTS_EXTRA.md >> AGENTS.md
alias b := build

# Run hook unit tests.
@test-hooks:
    {{ uv }} run python -m unittest \
        hooks/UserPromptSubmit/copy_prompt_to_clipboard_test.py \
        hooks/PreCommit/pre_commit_test.py

# Run flatten helper unit tests.
@test-flatten:
    {{ uv }} run python -m unittest helpers/flatten_test.py

# Run all tests.
@test: test-hooks test-flatten
