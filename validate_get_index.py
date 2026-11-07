#!/usr/bin/env python3
"""Validate the canonical GET source-index JSONL batches without editing the Vault."""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

from get_index_incremental import ScanError, strip_url_query_parameters


DEFAULT_VAULT = Path("/Users/nat/Documents/Obsidian Vault")
EXPECTED_KEYS = [
    "relative_path",
    "uid",
    "title",
    "created_date",
    "note_type",
    "domain",
    "sensitivity",
    "tags",
    "review_state",
    "basis",
    "flag",
]
TOPICS = {
    "AI与自动化",
    "产品与服务",
    "项目与流程",
    "战略与经营",
    "运营与增长",
    "组织与人才",
    "调研与洞察",
    "成长与生活",
}
DOMAINS = {"生活", "工作/个人", "工作/团队"}
SENSITIVITIES = {"普通", "内部", "受限"}
NOTE_TYPES = {"recorder_audio", "local_audio", "link"}
REVIEW_STATES = {"规则初分", "已核验", "待复核"}
BASES = {"metadata", "summary"}
FLAGS = {
    "none",
    "stale",
    "hr",
    "mixed",
    "client",
    "case",
    "student",
    "private",
    "medical",
    "duplicate",
    "summary_missing",
}
QUERY_URL = re.compile(
    r"https?://[^\s\]\[<>\"']+\?[^\s\]\[<>\"']*",
    re.IGNORECASE,
)


def scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def read_frontmatter(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        if handle.readline().rstrip("\r\n") != "---":
            raise ValueError("missing opening frontmatter delimiter")
        for line_number, line in enumerate(handle, start=2):
            stripped = line.rstrip("\r\n")
            if stripped == "---":
                return data
            if line_number > 200:
                raise ValueError("frontmatter exceeds 200 lines")
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", stripped)
            if match:
                data[match.group(1)] = scalar(match.group(2))
    raise ValueError("missing closing frontmatter delimiter")


def load_rows(paths: list[Path]) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:  # pragma: no cover - operational guard
            errors.append(f"{path}: cannot read: {exc}")
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                errors.append(f"{path}:{line_number}: blank JSONL line")
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{path}:{line_number}: invalid JSON: {exc.msg}")
                continue
            if not isinstance(row, dict):
                errors.append(f"{path}:{line_number}: row must be an object")
                continue
            row["__origin"] = f"{path}:{line_number}"
            rows.append(row)
    return rows, errors


def validate(
    paths: list[Path],
    vault_root: Path = DEFAULT_VAULT,
) -> tuple[list[dict], list[str]]:
    rows, errors = load_rows(paths)
    get_root = vault_root / "GET NOTES" / "Notes"
    source_paths = sorted(get_root.rglob("*.md"))
    expected_rel = {str(path.relative_to(vault_root)) for path in source_paths}
    path_counts: collections.Counter[str] = collections.Counter()
    uid_counts: collections.Counter[str] = collections.Counter()

    for row in rows:
        origin = row.pop("__origin")
        if list(row) != EXPECTED_KEYS:
            errors.append(f"{origin}: keys/order differ: {list(row)}")
            continue

        rel = row["relative_path"]
        uid = row["uid"]
        if not isinstance(rel, str) or rel.startswith("/") or ".." in Path(rel).parts:
            errors.append(f"{origin}: unsafe relative_path {rel!r}")
            continue
        if not isinstance(uid, str) or not uid:
            errors.append(f"{origin}: uid must be a non-empty string")
            continue
        path_counts[rel] += 1
        uid_counts[uid] += 1

        if rel not in expected_rel:
            errors.append(f"{origin}: path is not a current GET Markdown source: {rel}")
            continue
        source_path = vault_root / rel
        try:
            fm = read_frontmatter(source_path)
        except Exception as exc:
            errors.append(f"{origin}: cannot parse source frontmatter: {exc}")
            continue

        expected_date = fm.get("created", "")[:10]
        source_title = fm.get("title")
        try:
            expected_title = (
                strip_url_query_parameters(source_title)
                if source_title is not None
                else None
            )
        except ScanError:
            errors.append(f"{origin}: cannot safely normalize source title for {rel}")
            continue
        comparisons = {
            "uid": fm.get("uid"),
            "title": expected_title,
            "created_date": expected_date,
            "note_type": fm.get("note_type"),
        }
        for key, expected in comparisons.items():
            if row[key] != expected:
                if key == "title":
                    errors.append(f"{origin}: title mismatch for {rel}")
                else:
                    errors.append(
                        f"{origin}: {key} mismatch for {rel}: {row[key]!r} != {expected!r}"
                    )

        if row["note_type"] not in NOTE_TYPES:
            errors.append(f"{origin}: invalid note_type {row['note_type']!r}")
        if row["domain"] not in DOMAINS:
            errors.append(f"{origin}: invalid domain {row['domain']!r}")
        if row["sensitivity"] not in SENSITIVITIES:
            errors.append(f"{origin}: invalid sensitivity {row['sensitivity']!r}")
        tags = row["tags"]
        if not isinstance(tags, list) or not 1 <= len(tags) <= 2:
            errors.append(f"{origin}: tags must contain 1-2 topics")
        elif len(set(tags)) != len(tags) or any(tag not in TOPICS for tag in tags):
            errors.append(f"{origin}: duplicate or uncontrolled topic in {tags!r}")
        if row["review_state"] not in REVIEW_STATES:
            errors.append(f"{origin}: invalid review_state {row['review_state']!r}")
        if row["basis"] not in BASES:
            errors.append(f"{origin}: invalid basis {row['basis']!r}")
        if row["flag"] not in FLAGS:
            errors.append(f"{origin}: invalid flag {row['flag']!r}")
        if row["review_state"] == "已核验" and row["basis"] != "summary":
            errors.append(f"{origin}: 已核验 must use summary basis")
        if row["review_state"] == "规则初分" and row["basis"] != "metadata":
            errors.append(f"{origin}: 规则初分 must use metadata basis")

        serialized = json.dumps(row, ensure_ascii=False)
        if QUERY_URL.search(serialized):
            errors.append(f"{origin}: contains a URL with query parameters")
        for key, value in row.items():
            if key == "tags":
                continue
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                errors.append(f"{origin}: {key} must be a single-line string")

    missing = sorted(expected_rel - set(path_counts))
    extra = sorted(set(path_counts) - expected_rel)
    duplicate_paths = sorted(path for path, count in path_counts.items() if count != 1)
    duplicate_uids = sorted(uid for uid, count in uid_counts.items() if count != 1)
    if len(rows) != len(expected_rel):
        errors.append(f"record count {len(rows)} != GET Markdown count {len(expected_rel)}")
    if missing:
        errors.append(f"missing source paths ({len(missing)}): {missing[:10]}")
    if extra:
        errors.append(f"unexpected source paths ({len(extra)}): {extra[:10]}")
    if duplicate_paths:
        errors.append(f"non-unique source paths ({len(duplicate_paths)}): {duplicate_paths[:10]}")
    if duplicate_uids:
        errors.append(f"non-unique UIDs ({len(duplicate_uids)}): {duplicate_uids[:10]}")
    return rows, errors


def print_summary(rows: list[dict]) -> None:
    print(f"records={len(rows)}")
    for field in ("note_type", "domain", "sensitivity", "review_state", "basis", "flag"):
        counts = collections.Counter(row.get(field) for row in rows)
        rendered = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
        print(f"{field}: {rendered}")
    topics = collections.Counter(tag for row in rows for tag in row.get("tags", []))
    print("topics: " + ", ".join(f"{key}={topics[key]}" for key in sorted(topics)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("jsonl", nargs="+", type=Path)
    args = parser.parse_args()
    rows, errors = validate(args.jsonl, args.vault_root)
    print_summary(rows)
    if errors:
        print(f"FAIL errors={len(errors)}", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("PASS full GET source-index validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
