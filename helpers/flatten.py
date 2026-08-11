#!/usr/bin/env -S uv run python
"""
Flatten @file references in Markdown documents.
See https://github.com/openai/agents.md/issues/11#issuecomment-3366858928
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path
from typing import Tuple

REF_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)@(?P<path>\S+)[ \t]*(?:\r\n|\r|\n|$)",
    re.MULTILINE,
)


def flatten_file(path: Path, ancestors: Tuple[Path, ...] = ()) -> str:
    """Flatten @file references in the given file recursively."""
    resolved = path.resolve()
    if resolved in ancestors:
        cycle = " -> ".join(str(p) for p in ancestors + (resolved,))
        raise RuntimeError(f"Detected circular reference: {cycle}")

    try:
        raw_content = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Referenced file not found: {resolved}") from exc

    def _replace(match: re.Match[str]) -> str:
        indent = match.group("indent")
        target_token = match.group("path")
        target_path = (resolved.parent / target_token).resolve()
        flattened_text = flatten_file(target_path, ancestors + (resolved,))
        newline_consumed = match.group(0).endswith(("\r\n", "\n", "\r"))

        # Preserve indentation context for inlined blocks.
        flattened_text = (
            textwrap.indent(flattened_text, indent) if indent else flattened_text
        )

        if newline_consumed and not flattened_text.endswith(("\n", "\r")):
            # Re-introduce the newline we consumed from the placeholder line so
            # subsequent content stays on its own line.
            flattened_text += "\n"

        return flattened_text

    return REF_PATTERN.sub(_replace, raw_content)


def build_output_path(source: Path) -> Path:
    """Return the destination file path for a flattened document."""
    if source.name == "AGENTS_symlink.md":
        return source.with_name("AGENTS.md")

    suffix = source.suffix or ".md"
    return source.with_name(f"{source.stem}_flattened{suffix}")


def process_file(path: Path, dry_run: bool, output: Path | None = None) -> None:
    output_path = output or build_output_path(path)
    if not dry_run:
        same_path = output_path.resolve() == path.resolve()
        same_file = output_path.exists() and path.exists() and output_path.samefile(path)
        if same_path or same_file:
            raise ValueError(f"refusing to overwrite input file: {path}")

    flattened = flatten_file(path)
    if dry_run:
        sys.stdout.write(flattened)
        return

    output_path.write_text(flattened, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Flatten @file references in Markdown files."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Input files to flatten.",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print flattened content to stdout instead of writing a file (single file only).",
    )
    output_group.add_argument(
        "--output",
        type=Path,
        help="Write one flattened input to this path.",
    )
    args = parser.parse_args(argv)
    files = args.files or [Path("AGENTS_symlink.md")]

    if args.dry_run and len(files) != 1:
        parser.error("--dry-run requires exactly one input file")
    if args.output is not None and len(args.files) != 1:
        parser.error("--output requires exactly one input file")

    for file_path in files:
        try:
            process_file(file_path, dry_run=args.dry_run, output=args.output)
        except ValueError as error:
            parser.error(str(error))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
