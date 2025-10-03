#!/usr/bin/env python3
"""
Flatten @file references in Markdown documents.
See https://github.com/openai/agents.md/issues/11#issuecomment-3366858928
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
import re
from typing import Tuple

REF_PATTERN = re.compile(r"^(?P<indent>\s*)@(?P<path>[^\s]+)\s*$", re.MULTILINE)


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
        # Preserve indentation context for inlined blocks.
        return textwrap.indent(flattened_text, indent) if indent else flattened_text

    return REF_PATTERN.sub(_replace, raw_content)


def build_output_path(source: Path) -> Path:
    """Return the destination file path for a flattened document."""
    if source.name == "AGENTS_symlink.md":
        return source.with_name("AGENTS.md")

    suffix = source.suffix or ".md"
    return source.with_name(f"{source.stem}_flattened{suffix}")


def process_file(path: Path, dry_run: bool) -> None:
    flattened = flatten_file(path)
    if dry_run:
        sys.stdout.write(flattened)
        return

    output_path = build_output_path(path)
    output_path.write_text(flattened, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Flatten @file references in Markdown files."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        default=[Path("AGENTS_symlink.md")],
        help="Files to process in place.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print flattened content to stdout instead of writing a file (single file only).",
    )
    args = parser.parse_args(argv)

    if args.dry_run and len(args.files) != 1:
        parser.error("--dry-run requires exactly one input file")

    for file_path in args.files:
        process_file(file_path, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
