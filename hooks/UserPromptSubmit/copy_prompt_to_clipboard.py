#!/usr/bin/env python3
"""Copy submitted Codex prompts to the macOS clipboard for Raycast history.

This UserPromptSubmit hook mirrors each prompt into the system clipboard via
``pbcopy`` so it appears in Raycast's clipboard history. Raw prompts can be
noisy, so the text is sanitized first: paste/image markers are normalized,
fenced code blocks are collapsed, and oversized content is bounded.

Codex can add UserPromptSubmit stdout to developer context, so this hook writes
nothing to stdout. Warnings go to stderr and every error path exits 0 to avoid
breaking prompt submission.

Set ``CODEX_CLIP_DEBUG=1`` to append raw stdin to ``.debug.jsonl`` next to this
script for checking how Codex represents prompts and pasted content.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PBCOPY = "/usr/bin/pbcopy"

LONG_LINE_CHARS = 400
MAX_CHARS = 1500
MAX_LINES = 20

CLAUDE_PASTE_MARKER_RE = re.compile(
    r"\[(?:Pasted text|Image|\.\.\.Truncated text) #\d+(?: \+\d+ lines)?\.*\]"
)
CODEX_PASTED_CONTENT_RE = re.compile(r"\[Pasted Content [\d,]+ chars\]")
CODEX_IMAGE_MARKER_RE = re.compile(
    r"\[codex-clipboard-[^\]\n]*\.png \d+x\d+\]"
)
FENCE_RE = re.compile(
    r"^[ \t]*(`{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
UNTERMINATED_FENCE_RE = re.compile(
    r"^[ \t]*`{3,}.*\Z",
    re.MULTILINE | re.DOTALL,
)
BLANK_LINES_RE = re.compile(r"\n{3,}")


def _collapse_size(text: str) -> str:
    """Collapse oversized content without losing the useful prompt head."""
    lines = [
        "[Pasted]" if len(line) > LONG_LINE_CHARS else line
        for line in text.split("\n")
    ]
    text = "\n".join(lines)

    if len(lines) > MAX_LINES or len(text) > MAX_CHARS:
        head = "\n".join(lines[:MAX_LINES])[:MAX_CHARS].rstrip()
        text = f"{head} … [Pasted]"

    return text


def sanitize_prompt(prompt: str) -> str:
    """Sanitize a submitted prompt for clipboard history."""
    text = CLAUDE_PASTE_MARKER_RE.sub("Pasted", prompt)
    text = CODEX_PASTED_CONTENT_RE.sub("Pasted", text)
    text = CODEX_IMAGE_MARKER_RE.sub("Pasted", text)
    text = FENCE_RE.sub("[code]", text)
    text = UNTERMINATED_FENCE_RE.sub("[code]", text)
    text = BLANK_LINES_RE.sub("\n\n", text).strip()
    if not text:
        return ""
    text = _collapse_size(text)
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _maybe_debug(raw: str) -> None:
    """Append raw stdin to a debug log when CODEX_CLIP_DEBUG=1."""
    if os.environ.get("CODEX_CLIP_DEBUG") != "1":
        return

    try:
        debug_path = Path(__file__).parent / ".debug.jsonl"
        with debug_path.open("a", encoding="utf-8") as fh:
            fh.write(raw)
            if not raw.endswith("\n"):
                fh.write("\n")
    except OSError:
        pass


def main() -> None:
    """Run the hook."""
    raw = sys.stdin.read()
    _maybe_debug(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    if not isinstance(data, dict):
        sys.exit(0)

    prompt = data.get("prompt", "")
    if not isinstance(prompt, str):
        sys.exit(0)

    text = sanitize_prompt(prompt)
    if not text:
        sys.exit(0)

    try:
        result = subprocess.run(
            [PBCOPY],
            input=text,
            encoding="utf-8",
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Warning: pbcopy failed: {exc}", file=sys.stderr)
        sys.exit(0)

    if result.returncode != 0:
        print(
            f"Warning: pbcopy exited {result.returncode}: {result.stderr.strip()}",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
