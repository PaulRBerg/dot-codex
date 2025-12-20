# See https://just.systems/man/en/settings.html
set allow-duplicate-recipes
set allow-duplicate-variables
set shell := ["bash", "-euo", "pipefail", "-c"]
set unstable

# ---------------------------------------------------------------------------- #
#                                 DEPENDENCIES                                 #
# ---------------------------------------------------------------------------- #

python3 := require("python3")

# ---------------------------------------------------------------------------- #
#                                   COMMANDS                                   #
# ---------------------------------------------------------------------------- #

@default:
    just build

# Run the flatten script; accepts a `files` arg mirroring flatten.py
[private]
@flatten files="AGENTS_symlink.md" *args:
    python3 ~/.codex/flatten.py {{ files }} {{ args }}

# Build AGENTS.md by flattening and appending extra context.
@build:
    just flatten
    cat context/AGENTS_EXTRA.md >> AGENTS.md
alias b := build
