#!/usr/bin/env python3
"""Preview dynamic GET coverage-index pages as an apply_patch payload."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from get_index_render import render_index_pages, validate_index_pages


def read_rows(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


def emit_add(path: Path, content: str) -> None:
    print(f"*** Add File: {path}")
    for line in content.splitlines():
        print("+" + line)
    if not content.endswith("\n"):
        print("+")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True, nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    rows = read_rows(args.records)
    pages = render_index_pages(rows)
    validate_index_pages(pages, rows)
    print("*** Begin Patch")
    for filename, content in pages.items():
        emit_add(args.output_dir / filename, content)
    print("*** End Patch")


if __name__ == "__main__":
    main()
