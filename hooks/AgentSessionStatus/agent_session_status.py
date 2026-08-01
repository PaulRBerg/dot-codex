#!/usr/bin/env -S uv run python
"""Track live Codex turns and report cross-client agent-session status."""

from _agent_session_status.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
