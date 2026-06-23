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

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PBCOPY = "/usr/bin/pbcopy"

LONG_LINE_CHARS = 400
MAX_CHARS = 1500
MAX_LINES = 20
MIN_PROMPT_CHARS = 120
SHORT_ID_CHARS = 8

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
METADATA_VALUE_RE = re.compile(r"[^A-Za-z0-9._/@:-]+")
UUID_RE = re.compile(r"\b([0-9a-fA-F]{8})-[0-9a-fA-F-]{8,}\b")
GIT_REMOTE_PATH_RE = re.compile(r"[:/]([^/:]+?)(?:\.git)?/?$")
SUBAGENT_NOTIFICATION_RE = re.compile(r"^\s*<subagent_notification\b", re.IGNORECASE)
SUBAGENT_CONTROL_PROMPT_RE = re.compile(
    r"^\s*(?:please\s+)?(?:stop|interrupt|pause|cancel|halt)\b"
    r"(?=.*\b(?:return|report|summarize|give)\b)"
    r"(?=.*\b(?:current|findings|results|sources)\b)"
    r"(?=.*\bno edits\b)",
    re.IGNORECASE | re.DOTALL,
)

CWD_KEYS = (
    "cwd",
    "working_dir",
    "workingDirectory",
    "workspace",
    "workspace_root",
    "workspaceRoot",
    "project_path",
    "projectPath",
)
THREAD_ID_KEYS = (
    "thread_id",
    "threadId",
    "session_id",
    "sessionId",
    "conversation_id",
    "conversationId",
)
NESTED_METADATA_KEYS = ("thread", "session", "conversation", "turn", "workspace")
PROMPT_SOURCE_KEYS = (
    "actor",
    "author",
    "event_source",
    "eventSource",
    "origin",
    "originator",
    "prompt_kind",
    "promptKind",
    "prompt_source",
    "promptSource",
    "role",
    "sender",
    "session_kind",
    "sessionKind",
    "source",
    "thread_source",
    "threadSource",
)
NESTED_SOURCE_KEYS = (*NESTED_METADATA_KEYS, "message", "metadata")
NON_HUMAN_SOURCE_VALUES = {
    "agent",
    "assistant",
    "codex-agent",
    "delegated_subagent",
    "developer",
    "internal",
    "multi_agent",
    "multi_agent_v1",
    "multi_agent_v2",
    "subagent",
    "system",
    "tool",
    "worker",
}
NON_HUMAN_SOURCE_PARTS = ("multi_agent", "subagent", "delegated_subagent")


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


def _strip_pasted_and_code(prompt: str) -> str:
    """Remove pasted content and code blocks, leaving only typed prose."""
    text = CLAUDE_PASTE_MARKER_RE.sub("", prompt)
    text = CODEX_PASTED_CONTENT_RE.sub("", text)
    text = CODEX_IMAGE_MARKER_RE.sub("", text)
    text = FENCE_RE.sub("", text)
    text = UNTERMINATED_FENCE_RE.sub("", text)
    kept = [line for line in text.split("\n") if len(line) <= LONG_LINE_CHARS]
    return "\n".join(kept).strip()


def _should_copy_prompt(prompt: str) -> bool:
    """Return whether the typed prose is long enough to keep in history.

    The minimum-length threshold is measured only after pasted content and code
    blocks are stripped out, so a short message wrapped around a large paste is
    not kept just because the paste inflates the character count.
    """
    return len(_strip_pasted_and_code(prompt)) >= MIN_PROMPT_CHARS


def _metadata_strings(data: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """Return non-empty string metadata values at top level or one level deep."""
    values = []
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())

    for parent_key in NESTED_SOURCE_KEYS:
        parent = data.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for key in keys:
            value = parent.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())

    return values


def _has_non_human_source(data: dict[str, Any]) -> bool:
    """Return whether hook metadata identifies an internal/agent prompt source."""
    for value in _metadata_strings(data, PROMPT_SOURCE_KEYS):
        normalized = _safe_metadata_value(value, 64).lower()
        if normalized in NON_HUMAN_SOURCE_VALUES:
            return True
        if any(part in normalized for part in NON_HUMAN_SOURCE_PARTS):
            return True
    return False


def _should_skip_prompt_event(prompt: str, data: dict[str, Any]) -> bool:
    """Return whether this prompt event is internal rather than user-authored."""
    if _has_non_human_source(data):
        return True
    if SUBAGENT_NOTIFICATION_RE.match(prompt):
        return True
    return bool(SUBAGENT_CONTROL_PROMPT_RE.match(prompt))


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first non-empty string found at top level or one level deep."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for parent_key in NESTED_METADATA_KEYS:
        parent = data.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for key in keys:
            value = parent.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""


def _safe_metadata_value(value: str, max_chars: int) -> str:
    """Normalize a metadata value for a compact bracketed prefix."""
    text = re.sub(r"\s+", "-", value.strip())
    text = METADATA_VALUE_RE.sub("", text)
    return text[:max_chars].strip("-")


def _short_identifier(value: str) -> str:
    """Return a short readable identifier from a thread/session-ish value."""
    match = UUID_RE.search(value)
    if match:
        return match.group(1).lower()

    text = value.removeprefix("codex://threads/")
    return _safe_metadata_value(text, SHORT_ID_CHARS)


def _session_cwd(data: dict[str, Any]) -> Path:
    """Return the session cwd from hook data when present, otherwise process cwd."""
    value = _first_string(data, CWD_KEYS)
    if not value:
        return Path.cwd()

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _git_output(cwd: Path, *args: str) -> str:
    """Run a short git query and return stripped stdout, or empty on failure."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(cwd), *args],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _repo_root(cwd: Path) -> Path:
    """Return the git root for cwd, falling back to cwd."""
    root = _git_output(cwd, "rev-parse", "--show-toplevel")
    if root:
        return Path(root)
    return cwd


def _repo_label(cwd: Path, repo_root: Path) -> str:
    """Return a compact repo label, preferring remote origin over folder name."""
    remote = _git_output(cwd, "config", "--get", "remote.origin.url")
    match = GIT_REMOTE_PATH_RE.search(remote)
    if match:
        label = _safe_metadata_value(match.group(1), 32)
        if label:
            return label

    return _safe_metadata_value(repo_root.name or str(repo_root), 32)


def _path_reference(path: Path) -> str:
    """Return a short stable reference for non-git directories."""
    try:
        value = str(path.resolve())
    except OSError:
        value = str(path)
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:SHORT_ID_CHARS]


def build_metadata_prefix(data: dict[str, Any]) -> str:
    """Build a short provenance prefix for clipboard history."""
    cwd = _session_cwd(data)
    repo_root = _repo_root(cwd)
    project = _repo_label(cwd, repo_root)

    parts = []
    if project:
        parts.append(f"repo:{project}")

    thread_id = _short_identifier(_first_string(data, THREAD_ID_KEYS))
    if thread_id:
        parts.append(f"thread:{thread_id}")
    else:
        ref = _git_output(cwd, "rev-parse", "--short=8", "HEAD")
        parts.append(f"ref:{ref or _path_reference(repo_root)}")

    return f"[{' '.join(parts)}]" if parts else ""


def format_clipboard_prompt(prompt: str, data: dict[str, Any]) -> str:
    """Sanitize a prompt and prepend compact source metadata."""
    if _should_skip_prompt_event(prompt, data):
        return ""

    text = sanitize_prompt(prompt)
    if not text or not _should_copy_prompt(prompt):
        return ""

    prefix = build_metadata_prefix(data)
    return f"{prefix}\n{text}" if prefix else text


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
    if _should_skip_prompt_event(prompt, data):
        sys.exit(0)

    try:
        text = format_clipboard_prompt(prompt, data)
    except Exception as exc:  # noqa: BLE001 - hook must fail open.
        print(f"Warning: metadata prefix failed: {exc}", file=sys.stderr)
        sanitized = sanitize_prompt(prompt)
        text = sanitized if _should_copy_prompt(prompt) else ""
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
