#!/usr/bin/env python3
"""Detect changes in GET Markdown sources without writing to the Vault."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
import re
import secrets
import stat as stat_module
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from get_index_render import (
    INDEX_PAGE_FILENAMES,
    IndexRenderError,
    render_index_pages,
    validate_source_relative_path,
    validate_index_pages,
)
from get_index_transaction import (
    TransactionError,
    recover_transaction,
    replace_files_transactionally,
    transaction_lock,
)


SNAPSHOT_KEYS = (
    "relative_path",
    "uid",
    "title",
    "created_date",
    "note_type",
    "source_tags_sha256",
    "sha256",
)
INDEX_METADATA_KEYS = ("uid", "title", "created_date", "note_type", "source_tags_sha256")
CHANGE_CATEGORIES = (
    "added",
    "index_metadata_changed",
    "content_only_changed",
    "missing",
    "unchanged",
)
FRONTMATTER_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")
UID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_NOTE_TYPES = {"recorder_audio", "local_audio", "link"}
CANONICAL_KEYS = (
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
)
ALLOWED_DOMAINS = {"生活", "工作/个人", "工作/团队"}
ALLOWED_SENSITIVITIES = {"普通", "内部", "受限"}
ALLOWED_TOPICS = {
    "AI与自动化",
    "产品与服务",
    "项目与流程",
    "战略与经营",
    "运营与增长",
    "组织与人才",
    "调研与洞察",
    "成长与生活",
}
ALLOWED_REVIEW_STATES = {"规则初分", "已核验", "待复核"}
ALLOWED_BASES = {"metadata", "summary"}
ALLOWED_FLAGS = {
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
REVIEW_BUNDLE_KEYS = {
    "schema_version",
    "status",
    "accepted_fingerprint",
    "current_fingerprint",
    "records_fingerprint",
    "drafts",
    "notices",
}
REVIEW_DRAFT_KEYS = {
    "change_kind",
    "relative_path",
    "uid",
    "title",
    "created_date",
    "note_type",
    "source_tags_sha256",
    "domain",
    "sensitivity",
    "tags",
    "review_state",
    "basis",
    "flag",
    "unresolved_fields",
}
REVIEW_NOTICE_KEYS = {"kind", "relative_path", "uid", "note_type", "action"}
FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
URL_TOKEN = re.compile(r"https?://\S+", re.IGNORECASE)
QUERY_URL = re.compile(r"https?://\S*\?\S*", re.IGNORECASE)


class ScanError(ValueError):
    """A safe, user-facing validation error."""


def strip_url_query_parameters(value: str) -> str:
    """Remove query parameters from URLs before a source-derived value is persisted."""

    def replace(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        if "?" not in raw_url:
            return raw_url
        try:
            parsed = urlsplit(raw_url)
        except ValueError as exc:
            raise ScanError("GET 来源标题包含不安全 URL") from exc
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))

    sanitized = URL_TOKEN.sub(replace, value)
    if QUERY_URL.search(sanitized):
        raise ScanError("GET 来源标题包含无法移除查询参数的 URL")
    return sanitized


def reject_query_urls(values: list[str], origin: str) -> None:
    if any(QUERY_URL.search(value) for value in values):
        raise ScanError(f"{origin}: 持久化字段包含带查询参数的 URL")


def scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_tag_string(value: str, display_path: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            tag = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ScanError(f"{display_path}: invalid quoted tag") from exc
    elif value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise ScanError(f"{display_path}: invalid quoted tag")
        inner = value[1:-1]
        if "'" in inner.replace("''", ""):
            raise ScanError(f"{display_path}: invalid quoted tag")
        tag = inner.replace("''", "'")
    else:
        if (not value or value[:1] in "[]{}&,*!|>#%@`" or value in {"-", "?", ":"}
                or value.startswith(("- ", "? ", ": ")) or ": " in value or " #" in value):
            raise ScanError(f"{display_path}: unsupported tag")
        tag = value
    if not isinstance(tag, str) or not tag.strip() or any(ord(c) < 32 for c in tag):
        raise ScanError(f"{display_path}: invalid tag")
    return tag


def parse_inline_tags(value: str, display_path: str) -> list[str]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise ScanError(f"{display_path}: tags must use a supported inline list")
    inner = value[1:-1].strip()
    if not inner:
        return []
    # Tokenize the supported string-only YAML subset; decode with the same
    # routine as block tags so escapes and doubled apostrophes retain meaning.
    pattern = re.compile(r'\s*("(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\'|[^,\[\]{}]+?)\s*(,|$)')
    tags: list[str] = []
    pos = 0
    while pos < len(inner):
        match = pattern.match(inner, pos)
        if not match:
            raise ScanError(f"{display_path}: unsupported inline tag")
        tags.append(parse_tag_string(match.group(1), display_path))
        pos = match.end()
        if match.group(2) == "," and pos == len(inner):
            raise ScanError(f"{display_path}: tags contain an empty value")
    return sorted(set(tags))


def source_tags_sha256(frontmatter: dict[str, str], display_path: str) -> str:
    tags = parse_inline_tags(frontmatter.get("tags", "[]"), display_path)
    canonical = json.dumps(tags, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_uid(value: str, origin: str) -> str:
    if not UID_PATTERN.fullmatch(value):
        raise ScanError(f"{origin}: uid has an invalid format")
    return value


def validate_note_type(value: str, origin: str) -> str:
    if value not in ALLOWED_NOTE_TYPES:
        raise ScanError(f"{origin}: note_type is not in the supported enum")
    return value


def validate_created_date(value: str, origin: str) -> str:
    if not DATE_PATTERN.fullmatch(value):
        raise ScanError(f"{origin}: created date has an invalid format")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ScanError(f"{origin}: created date is not a valid calendar date") from exc
    return value


def created_date_from_frontmatter(value: str, origin: str) -> str:
    if len(value) > 10 and value[10] not in {" ", "T"}:
        raise ScanError(f"{origin}: created has an invalid timestamp format")
    return validate_created_date(value[:10], origin)


def read_frontmatter(path: Path, display_path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    block_tags: list[str] | None = None
    block_indent: int | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            if handle.readline().rstrip("\r\n") != "---":
                raise ScanError(f"{display_path}: missing opening frontmatter delimiter")
            for line_number, line in enumerate(handle, start=2):
                stripped = line.rstrip("\r\n")
                if stripped == "---":
                    if block_tags is not None and not block_tags:
                        raise ScanError(f"{display_path}: tags block is empty")
                    return data
                if line_number > 200:
                    raise ScanError(f"{display_path}: frontmatter exceeds 200 lines")
                if not stripped or stripped.lstrip().startswith("#"):
                    continue
                if block_tags is not None:
                    item = re.fullmatch(r"( +)- (.+)", stripped)
                    if item:
                        indent = len(item.group(1))
                        if block_indent is not None and indent != block_indent:
                            raise ScanError(f"{display_path}: tags block indentation is inconsistent")
                        block_indent = indent
                        value = item.group(2).strip()
                        tag = parse_tag_string(value, display_path)
                        block_tags.append(tag)
                        data["tags"] = json.dumps(block_tags, ensure_ascii=False)
                        continue
                    if not block_tags:
                        raise ScanError(f"{display_path}: tags block is empty")
                    block_tags = None
                    block_indent = None
                match = FRONTMATTER_LINE.match(stripped)
                if not match:
                    raise ScanError(f"{display_path}: frontmatter contains an unsupported line")
                key = match.group(1)
                if key in data:
                    raise ScanError(f"{display_path}: duplicate frontmatter key")
                data[key] = scalar(match.group(2))
                if key == "tags" and not match.group(2).strip():
                    block_tags = []
                    data[key] = "[]"
    except UnicodeDecodeError as exc:
        raise ScanError(f"{display_path}: frontmatter is not valid UTF-8") from exc
    except OSError as exc:
        raise ScanError(f"{display_path}: cannot read source ({exc.__class__.__name__})") from exc
    raise ScanError(f"{display_path}: missing closing frontmatter delimiter")


def sha256_file(path: Path, display_path: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ScanError(f"{display_path}: cannot hash source ({exc.__class__.__name__})") from exc
    return digest.hexdigest()


def validate_relative_path(value: Any, origin: str) -> str:
    try:
        return validate_source_relative_path(value)
    except IndexRenderError as exc:
        raise ScanError(f"{origin}: {exc}") from exc


def validate_entry(raw: Any, origin: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ScanError(f"{origin}: snapshot source must be an object")
    if set(raw) != set(SNAPSHOT_KEYS):
        raise ScanError(f"{origin}: snapshot source fields do not match the allowed schema")
    entry = {key: raw[key] for key in SNAPSHOT_KEYS}
    entry["relative_path"] = validate_relative_path(entry["relative_path"], origin)
    for key in SNAPSHOT_KEYS[1:]:
        if not isinstance(entry[key], str) or not entry[key] or "\n" in entry[key] or "\r" in entry[key]:
            raise ScanError(f"{origin}: {key} must be a non-empty single-line string")
    entry["uid"] = validate_uid(entry["uid"], origin)
    entry["note_type"] = validate_note_type(entry["note_type"], origin)
    entry["created_date"] = validate_created_date(entry["created_date"], origin)
    for key in ("source_tags_sha256", "sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", entry[key]):
            raise ScanError(f"{origin}: {key} must be a lowercase SHA-256 digest")
    reject_query_urls(
        [entry[key] for key in SNAPSHOT_KEYS if key not in {"source_tags_sha256", "sha256"}],
        origin,
    )
    return entry


def load_snapshot_with_bytes(
    path: Path,
) -> tuple[dict[str, dict[str, str]], bytes]:
    content = read_regular_bytes(path, "accepted snapshot")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScanError("accepted snapshot cannot be read as valid JSON") from exc
    if not isinstance(payload, dict):
        raise ScanError("accepted snapshot must be a JSON object")
    if set(payload) != {"schema_version", "sources"}:
        raise ScanError("accepted snapshot fields do not match the allowed schema")
    if payload.get("schema_version") != 1:
        raise ScanError("accepted snapshot has an unsupported schema")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ScanError("accepted snapshot sources must be a list")
    if not raw_sources:
        raise ScanError("accepted snapshot must contain at least one source")

    by_path: dict[str, dict[str, str]] = {}
    uid_to_path: dict[str, str] = {}
    for index, raw in enumerate(raw_sources, start=1):
        entry = validate_entry(raw, f"snapshot source {index}")
        relative_path = entry["relative_path"]
        uid = entry["uid"]
        if relative_path in by_path:
            raise ScanError(f"accepted snapshot has duplicate path: {relative_path}")
        if uid in uid_to_path:
            raise ScanError(f"accepted snapshot has duplicate UID: {uid}")
        by_path[relative_path] = entry
        uid_to_path[uid] = relative_path
    return by_path, content


def load_snapshot(path: Path) -> dict[str, dict[str, str]]:
    snapshot, _ = load_snapshot_with_bytes(path)
    return snapshot


def list_source_files(source_root: Path) -> list[Path]:
    errors: list[str] = []
    paths: list[Path] = []

    def on_walk_error(exc: OSError) -> None:
        errors.append(f"cannot enumerate GET source directory ({exc.__class__.__name__})")

    for directory, dirnames, filenames in os.walk(
        source_root, topdown=True, followlinks=False, onerror=on_walk_error
    ):
        dirnames.sort()
        filenames.sort()
        kept_directories: list[str] = []
        for dirname in dirnames:
            child = Path(directory) / dirname
            if child.is_symlink():
                errors.append("symlinked GET source directories are not allowed")
            else:
                kept_directories.append(dirname)
        dirnames[:] = kept_directories
        for filename in filenames:
            child = Path(directory) / filename
            try:
                mode = child.lstat().st_mode
            except OSError as exc:
                errors.append(f"cannot inspect GET source entry ({exc.__class__.__name__})")
                continue
            if stat_module.S_ISLNK(mode):
                errors.append("symlinked GET source files are not allowed")
            elif child.suffix == ".md":
                if not stat_module.S_ISREG(mode):
                    errors.append("GET Markdown sources must be regular files")
                else:
                    paths.append(child)
    if errors:
        raise ScanError("\n".join(errors))
    return sorted(paths)


def stat_signature(path: Path, display_path: str) -> tuple[int, int, int, int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ScanError(f"{display_path}: cannot stat source ({exc.__class__.__name__})") from exc
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def scan_current_sources(vault_root: Path) -> dict[str, dict[str, str]]:
    source_root = vault_root / "GET NOTES" / "Notes"
    if not source_root.is_dir():
        raise ScanError("GET Markdown source directory does not exist")
    if any(path.is_symlink() for path in (vault_root, source_root.parent.parent, source_root.parent, source_root)):
        raise ScanError("symlinked Vault or GET source roots are not allowed")

    vault_resolved = vault_root.resolve()
    sources: dict[str, dict[str, str]] = {}
    scanned_signatures: dict[str, tuple[int, int, int, int, int]] = {}
    uid_to_path: dict[str, str] = {}
    errors: list[str] = []
    initial_paths = list_source_files(source_root)
    for path in initial_paths:
        try:
            resolved = path.resolve()
            try:
                relative_path = resolved.relative_to(vault_resolved).as_posix()
            except ValueError as exc:
                raise ScanError("source resolves outside the Vault") from exc
            relative_path = validate_relative_path(relative_path, relative_path)
            before = stat_signature(resolved, relative_path)
            frontmatter = read_frontmatter(resolved, relative_path)
            required = ("uid", "title", "created", "note_type")
            if any(not frontmatter.get(key) for key in required):
                raise ScanError(f"{relative_path}: frontmatter is missing required index fields")
            if any(
                any(ord(character) < 32 for character in frontmatter[key]) for key in required
            ):
                raise ScanError(f"{relative_path}: index frontmatter contains control characters")
            entry = {
                "relative_path": relative_path,
                "uid": validate_uid(frontmatter["uid"], relative_path),
                "title": strip_url_query_parameters(frontmatter["title"]),
                "created_date": created_date_from_frontmatter(frontmatter["created"], relative_path),
                "note_type": validate_note_type(frontmatter["note_type"], relative_path),
                "source_tags_sha256": source_tags_sha256(frontmatter, relative_path),
                "sha256": sha256_file(resolved, relative_path),
            }
            after = stat_signature(resolved, relative_path)
            if before != after:
                raise ScanError(f"{relative_path}: source changed while it was being scanned")
            scanned_signatures[relative_path] = after
            uid = entry["uid"]
            if uid in uid_to_path:
                raise ScanError("current GET sources contain a duplicate UID")
            uid_to_path[uid] = relative_path
            sources[relative_path] = entry
        except ScanError as exc:
            errors.append(str(exc))
    final_paths = list_source_files(source_root)
    if [path.resolve() for path in initial_paths] != [path.resolve() for path in final_paths]:
        errors.append("GET source tree changed while it was being scanned")
    else:
        for path in final_paths:
            resolved = path.resolve()
            relative_path = resolved.relative_to(vault_resolved).as_posix()
            if relative_path not in scanned_signatures:
                continue
            if stat_signature(resolved, relative_path) != scanned_signatures[relative_path]:
                errors.append(f"{relative_path}: source changed after it was scanned")
    if errors:
        raise ScanError("\n".join(errors))
    return sources


def change_pointer(entry: dict[str, str]) -> dict[str, str]:
    return {
        "relative_path": entry["relative_path"],
        "uid": entry["uid"],
        "note_type": entry["note_type"],
    }


def source_fingerprint(sources: dict[str, dict[str, str]]) -> str:
    stable = [sources[relative_path] for relative_path in sorted(sources)]
    serialized = json.dumps(stable, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compare_sources(
    accepted: dict[str, dict[str, str]], current: dict[str, dict[str, str]]
) -> dict[str, Any]:
    accepted_paths = set(accepted)
    current_paths = set(current)
    accepted_uid_paths = {entry["uid"]: path for path, entry in accepted.items()}
    current_uid_paths = {entry["uid"]: path for path, entry in current.items()}
    for relative_path in sorted(accepted_paths & current_paths):
        if accepted[relative_path]["uid"] != current[relative_path]["uid"]:
            raise ScanError(f"source identity changed at existing path: {relative_path}")
    for uid in sorted(set(accepted_uid_paths) & set(current_uid_paths)):
        if accepted_uid_paths[uid] != current_uid_paths[uid]:
            raise ScanError("source identity path changed for an existing UID")

    changes: list[dict[str, str]] = []
    counts = {category: 0 for category in CHANGE_CATEGORIES}

    for relative_path in sorted(current_paths - accepted_paths):
        changes.append({"kind": "added", **change_pointer(current[relative_path])})
        counts["added"] += 1
    for relative_path in sorted(accepted_paths - current_paths):
        changes.append({"kind": "missing", **change_pointer(accepted[relative_path])})
        counts["missing"] += 1
    for relative_path in sorted(accepted_paths & current_paths):
        before = accepted[relative_path]
        after = current[relative_path]
        if any(before[key] != after[key] for key in INDEX_METADATA_KEYS):
            changes.append({"kind": "index_metadata_changed", **change_pointer(after)})
            counts["index_metadata_changed"] += 1
        elif before["sha256"] != after["sha256"]:
            changes.append({"kind": "content_only_changed", **change_pointer(after)})
            counts["content_only_changed"] += 1
        else:
            counts["unchanged"] += 1

    changed_count = sum(counts[category] for category in CHANGE_CATEGORIES if category != "unchanged")
    return {
        "schema_version": 1,
        "status": "changes_detected" if changed_count else "unchanged",
        "accepted_source_count": len(accepted),
        "current_source_count": len(current),
        "accepted_fingerprint": source_fingerprint(accepted),
        "current_fingerprint": source_fingerprint(current),
        "counts": counts,
        "changes": changes,
    }


def read_regular_bytes(path: Path, label: str) -> bytes:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise ScanError(f"{label} does not exist") from exc
    except OSError as exc:
        raise ScanError(f"{label} cannot be inspected ({exc.__class__.__name__})") from exc
    if (
        stat_module.S_ISLNK(file_stat.st_mode)
        or not stat_module.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
    ):
        raise ScanError(f"{label} must be a single-hard-link regular file")
    if file_stat.st_size > 20 * 1024 * 1024:
        raise ScanError(f"{label} exceeds the supported size limit")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if (
                not stat_module.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_nlink != 1
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (file_stat.st_dev, file_stat.st_ino)
            ):
                raise ScanError(f"{label} changed while it was being opened")
            content = handle.read(20 * 1024 * 1024 + 1)
    except OSError as exc:
        raise ScanError(f"{label} cannot be read") from exc
    if len(content) > 20 * 1024 * 1024:
        raise ScanError(f"{label} exceeds the supported size limit")
    return content


def read_regular_text(path: Path, label: str) -> str:
    try:
        return read_regular_bytes(path, label).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScanError(f"{label} cannot be read as UTF-8") from exc


def load_scan_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(read_regular_text(path, "扫描报告"))
    except json.JSONDecodeError as exc:
        raise ScanError("扫描报告不是有效 JSON") from exc
    if not isinstance(report, dict):
        raise ScanError("扫描报告必须是 JSON 对象")
    return report


def validate_canonical_record(raw: Any, origin: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or tuple(raw) != CANONICAL_KEYS:
        raise ScanError(f"{origin}: 规范记录字段不符合允许的结构")
    record = dict(raw)
    record["relative_path"] = validate_relative_path(record["relative_path"], origin)
    for key in CANONICAL_KEYS:
        if key == "tags":
            continue
        if not isinstance(record[key], str) or not record[key] or any(
            character in record[key] for character in ("\n", "\r")
        ):
            raise ScanError(f"{origin}: 规范记录包含无效标量字段")
    record["uid"] = validate_uid(record["uid"], origin)
    record["created_date"] = validate_created_date(record["created_date"], origin)
    record["note_type"] = validate_note_type(record["note_type"], origin)
    if record["domain"] not in ALLOWED_DOMAINS:
        raise ScanError(f"{origin}: 规范记录 domain 不在支持的枚举内")
    if record["sensitivity"] not in ALLOWED_SENSITIVITIES:
        raise ScanError(f"{origin}: 规范记录 sensitivity 不在支持的枚举内")
    tags = record["tags"]
    if (
        not isinstance(tags, list)
        or not 1 <= len(tags) <= 2
        or any(not isinstance(tag, str) or tag not in ALLOWED_TOPICS for tag in tags)
        or len(set(tags)) != len(tags)
    ):
        raise ScanError(f"{origin}: 规范记录稳定主题无效")
    if record["review_state"] not in ALLOWED_REVIEW_STATES:
        raise ScanError(f"{origin}: 规范记录 review_state 不在支持的枚举内")
    if record["basis"] not in ALLOWED_BASES:
        raise ScanError(f"{origin}: 规范记录 basis 不在支持的枚举内")
    if record["flag"] not in ALLOWED_FLAGS:
        raise ScanError(f"{origin}: 规范记录 flag 不在支持的枚举内")
    if record["review_state"] == "已核验" and record["basis"] != "summary":
        raise ScanError(f"{origin}: 规范记录复核证据不一致")
    if record["review_state"] == "规则初分" and record["basis"] != "metadata":
        raise ScanError(f"{origin}: 规范记录复核证据不一致")
    reject_query_urls(
        [record[key] for key in CANONICAL_KEYS if key != "tags"],
        origin,
    )
    return record


def load_canonical_record_parts(
    paths: list[Path],
) -> tuple[
    dict[str, dict[str, Any]],
    list[list[dict[str, Any]]],
    dict[str, int],
    dict[Path, bytes],
]:
    by_path: dict[str, dict[str, Any]] = {}
    uid_to_path: dict[str, str] = {}
    part_by_path: dict[str, int] = {}
    parts: list[list[dict[str, Any]]] = []
    raw_contents: dict[Path, bytes] = {}
    record_number = 0
    for file_number, path in enumerate(paths, start=1):
        part: list[dict[str, Any]] = []
        raw_content = read_regular_bytes(path, f"规范记录文件 {file_number}")
        try:
            text = raw_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScanError(f"规范记录文件 {file_number} cannot be read as UTF-8") from exc
        raw_contents[path] = raw_content
        for line_number, line in enumerate(text.splitlines(), start=1):
            record_number += 1
            origin = f"规范记录 {record_number}"
            if not line.strip():
                raise ScanError(f"{origin}: 不允许空白 JSONL 行")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScanError(f"{origin}: 记录不是有效 JSON") from exc
            record = validate_canonical_record(raw, origin)
            relative_path = record["relative_path"]
            uid = record["uid"]
            if relative_path in by_path or uid in uid_to_path:
                raise ScanError("规范记录包含重复路径或 UID")
            by_path[relative_path] = record
            uid_to_path[uid] = relative_path
            part_by_path[relative_path] = file_number - 1
            part.append(record)
        parts.append(part)
    if not by_path:
        raise ScanError("规范记录至少需要一条记录")
    return by_path, parts, part_by_path, raw_contents


def load_canonical_records(paths: list[Path]) -> dict[str, dict[str, Any]]:
    records, _, _, _ = load_canonical_record_parts(paths)
    return records


def validate_records_match_snapshot(
    records: dict[str, dict[str, Any]], accepted: dict[str, dict[str, str]]
) -> None:
    if set(records) != set(accepted):
        raise ScanError("规范记录未完整覆盖已接受的增量快照")
    fact_keys = ("relative_path", "uid", "title", "created_date", "note_type")
    for relative_path, snapshot_entry in accepted.items():
        record = records[relative_path]
        if any(record[key] != snapshot_entry[key] for key in fact_keys):
            raise ScanError("规范记录与已接受的增量快照不一致")


def canonical_records_fingerprint(records: dict[str, dict[str, Any]]) -> str:
    stable = [records[relative_path] for relative_path in sorted(records)]
    serialized = json.dumps(stable, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def review_draft_facts(entry: dict[str, str], change_kind: str) -> dict[str, Any]:
    return {
        "change_kind": change_kind,
        "relative_path": entry["relative_path"],
        "uid": entry["uid"],
        "title": strip_url_query_parameters(entry["title"]),
        "created_date": entry["created_date"],
        "note_type": entry["note_type"],
        "source_tags_sha256": entry["source_tags_sha256"],
    }


def added_review_draft(
    entry: dict[str, str],
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if entry["relative_path"] in records or any(
        record["uid"] == entry["uid"] for record in records.values()
    ):
        raise ScanError("新增 GET 来源与已接受的规范记录冲突")
    return {
        **review_draft_facts(entry, "added"),
        "domain": None,
        "sensitivity": "受限",
        "tags": [],
        "review_state": "待复核",
        "basis": "metadata",
        "flag": None,
        "unresolved_fields": ["domain", "tags", "flag"],
    }


def metadata_review_draft(
    entry: dict[str, str],
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    try:
        record = records[entry["relative_path"]]
    except KeyError as exc:
        raise ScanError("索引元数据变化缺少已接受的规范记录") from exc
    if record["uid"] != entry["uid"]:
        raise ScanError("索引元数据变化的来源身份与规范记录不一致")
    return {
        **review_draft_facts(entry, "index_metadata_changed"),
        "domain": record["domain"],
        "sensitivity": record["sensitivity"],
        "tags": list(record["tags"]),
        "review_state": "待复核",
        "basis": record["basis"],
        "flag": record["flag"],
        "unresolved_fields": [],
    }


def build_review_bundle(
    accepted: dict[str, dict[str, str]],
    current: dict[str, dict[str, str]],
    report: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected_report = compare_sources(accepted, current)
    if report != expected_report:
        raise ScanError("扫描报告已失效、不完整或与当前 GET 来源不一致")
    drafts: list[dict[str, Any]] = []
    notices: list[dict[str, str]] = []
    for change in report["changes"]:
        kind = change["kind"]
        if kind == "added":
            drafts.append(added_review_draft(current[change["relative_path"]], records))
        elif kind == "index_metadata_changed":
            drafts.append(metadata_review_draft(current[change["relative_path"]], records))
        elif kind == "content_only_changed":
            notices.append({**change, "action": "keep_accepted_governance"})
        elif kind == "missing":
            raise ScanError("扫描报告包含缺失的 GET 来源；待复核草稿生成已阻止")
        else:
            raise ScanError("扫描报告包含待复核草稿尚不支持的变化类型")

    drafts.sort(key=lambda draft: (draft["relative_path"], draft["change_kind"]))
    notices.sort(key=lambda notice: (notice["relative_path"], notice["kind"]))
    return {
        "schema_version": 1,
        "status": "review_required" if drafts else "no_review_needed",
        "accepted_fingerprint": report["accepted_fingerprint"],
        "current_fingerprint": report["current_fingerprint"],
        "records_fingerprint": canonical_records_fingerprint(records),
        "drafts": drafts,
        "notices": notices,
    }


def validate_review_draft_entry(raw: Any, origin: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != REVIEW_DRAFT_KEYS:
        raise ScanError(f"{origin}: 待复核草稿字段不符合允许的结构")
    draft = dict(raw)
    if not isinstance(draft["change_kind"], str) or draft["change_kind"] not in {
        "added",
        "index_metadata_changed",
    }:
        raise ScanError(f"{origin}: 待复核草稿 change_kind 不受支持")
    draft["relative_path"] = validate_relative_path(draft["relative_path"], origin)
    if not isinstance(draft["uid"], str):
        raise ScanError(f"{origin}: 待复核草稿 UID 无效")
    draft["uid"] = validate_uid(draft["uid"], origin)
    if not isinstance(draft["title"], str) or not draft["title"] or any(
        character in draft["title"] for character in ("\n", "\r")
    ):
        raise ScanError(f"{origin}: 待复核草稿标题无效")
    if not isinstance(draft["created_date"], str):
        raise ScanError(f"{origin}: 待复核草稿 created_date 无效")
    draft["created_date"] = validate_created_date(draft["created_date"], origin)
    if not isinstance(draft["note_type"], str):
        raise ScanError(f"{origin}: 待复核草稿 note_type 无效")
    draft["note_type"] = validate_note_type(draft["note_type"], origin)
    if not isinstance(draft["source_tags_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", draft["source_tags_sha256"]
    ):
        raise ScanError(f"{origin}: 待复核草稿的 GET 原标签摘要无效")
    if draft["domain"] is not None and (
        not isinstance(draft["domain"], str) or draft["domain"] not in ALLOWED_DOMAINS
    ):
        raise ScanError(f"{origin}: 待复核草稿 domain 不在支持的枚举内")
    if not isinstance(draft["sensitivity"], str) or draft["sensitivity"] not in ALLOWED_SENSITIVITIES:
        raise ScanError(f"{origin}: 待复核草稿 sensitivity 不在支持的枚举内")
    tags = draft["tags"]
    if not isinstance(tags, list) or len(tags) > 2 or any(
        not isinstance(tag, str) or tag not in ALLOWED_TOPICS for tag in tags
    ):
        raise ScanError(f"{origin}: 待复核草稿稳定主题无效")
    if len(set(tags)) != len(tags):
        raise ScanError(f"{origin}: 待复核草稿稳定主题重复")
    if not isinstance(draft["review_state"], str) or draft["review_state"] not in ALLOWED_REVIEW_STATES:
        raise ScanError(f"{origin}: 待复核草稿 review_state 不在支持的枚举内")
    if not isinstance(draft["basis"], str) or draft["basis"] not in ALLOWED_BASES:
        raise ScanError(f"{origin}: 待复核草稿 basis 不在支持的枚举内")
    if draft["flag"] is not None and (
        not isinstance(draft["flag"], str) or draft["flag"] not in ALLOWED_FLAGS
    ):
        raise ScanError(f"{origin}: 待复核草稿 flag 不在支持的枚举内")
    if draft["review_state"] == "已核验" and draft["basis"] != "summary":
        raise ScanError(f"{origin}: 待复核草稿复核证据不一致")
    if draft["review_state"] == "规则初分" and draft["basis"] != "metadata":
        raise ScanError(f"{origin}: 待复核草稿复核证据不一致")
    expected_unresolved = [
        field
        for field, unresolved in (
            ("domain", draft["domain"] is None),
            ("tags", not draft["tags"]),
            ("flag", draft["flag"] is None),
        )
        if unresolved
    ]
    if draft["unresolved_fields"] != expected_unresolved:
        raise ScanError(f"{origin}: 待复核草稿 unresolved_fields 不一致")
    reject_query_urls(
        [
            draft["relative_path"],
            draft["uid"],
            draft["title"],
            draft["note_type"],
        ],
        origin,
    )
    return draft


def validate_review_bundle(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != REVIEW_BUNDLE_KEYS:
        raise ScanError("待复核草稿包字段不符合允许的结构")
    bundle = dict(raw)
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ScanError("待复核草稿包使用了不受支持的结构版本")
    for key in ("accepted_fingerprint", "current_fingerprint", "records_fingerprint"):
        if not isinstance(bundle[key], str) or not FINGERPRINT_PATTERN.fullmatch(bundle[key]):
            raise ScanError("待复核草稿包包含无效指纹")
    if not isinstance(bundle["drafts"], list) or not isinstance(bundle["notices"], list):
        raise ScanError("待复核草稿包列表无效")
    drafts = [
        validate_review_draft_entry(draft, f"待复核草稿 {index}")
        for index, draft in enumerate(bundle["drafts"], start=1)
    ]
    notices: list[dict[str, str]] = []
    for index, raw_notice in enumerate(bundle["notices"], start=1):
        origin = f"变化通知 {index}"
        if not isinstance(raw_notice, dict) or set(raw_notice) != REVIEW_NOTICE_KEYS:
            raise ScanError(f"{origin}: 字段不符合允许的结构")
        notice = dict(raw_notice)
        if notice["kind"] != "content_only_changed" or notice["action"] != "keep_accepted_governance":
            raise ScanError(f"{origin}: action 不受支持")
        notice["relative_path"] = validate_relative_path(notice["relative_path"], origin)
        if not isinstance(notice["uid"], str):
            raise ScanError(f"{origin}: UID 无效")
        notice["uid"] = validate_uid(notice["uid"], origin)
        if not isinstance(notice["note_type"], str):
            raise ScanError(f"{origin}: note_type 无效")
        notice["note_type"] = validate_note_type(notice["note_type"], origin)
        reject_query_urls(
            [notice["relative_path"], notice["uid"], notice["note_type"]],
            origin,
        )
        notices.append(notice)
    all_entries = [*drafts, *notices]
    entry_paths = [entry["relative_path"] for entry in all_entries]
    entry_uids = [entry["uid"] for entry in all_entries]
    if len(set(entry_paths)) != len(entry_paths) or len(set(entry_uids)) != len(entry_uids):
        raise ScanError("待复核草稿包包含重复路径或 UID")
    expected_status = "review_required" if drafts else "no_review_needed"
    if bundle["status"] != expected_status:
        raise ScanError("待复核草稿包 status 不一致")
    bundle["drafts"] = drafts
    bundle["notices"] = notices
    return bundle


def load_review_bundle_with_bytes(path: Path) -> tuple[dict[str, Any], bytes]:
    content = read_regular_bytes(path, "待复核草稿")
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScanError("待复核草稿不是有效 JSON") from exc
    return validate_review_bundle(raw), content


def load_review_bundle(path: Path) -> dict[str, Any]:
    bundle, _ = load_review_bundle_with_bytes(path)
    return bundle


def resolved_output_path(
    output: Path,
    vault_root: Path,
    protected_paths: list[Path],
) -> Path:
    try:
        output_stat = output.lstat()
    except FileNotFoundError:
        output_stat = None
    except OSError as exc:
        raise ScanError("无法检查待复核草稿输出") from exc
    if output_stat is not None and (
        stat_module.S_ISLNK(output_stat.st_mode)
        or not stat_module.S_ISREG(output_stat.st_mode)
        or output_stat.st_nlink != 1
    ):
        raise ScanError("待复核草稿输出必须是单硬链接普通文件")
    try:
        resolved = output.resolve(strict=False)
        vault_resolved = vault_root.resolve(strict=True)
    except OSError as exc:
        raise ScanError("无法解析待复核草稿输出路径") from exc
    candidate_chain = (resolved, *resolved.parents)
    physically_inside_vault = False
    for candidate in candidate_chain:
        try:
            if os.path.samefile(candidate, vault_resolved):
                physically_inside_vault = True
                break
        except (FileNotFoundError, OSError, ValueError):
            continue
    if (
        resolved == vault_resolved
        or vault_resolved in resolved.parents
        or physically_inside_vault
    ):
        raise ScanError("待复核草稿输出必须位于 Vault 外")
    for protected_path in protected_paths:
        protected_resolved = protected_path.resolve(strict=False)
        same_physical_file = False
        try:
            same_physical_file = os.path.samefile(resolved, protected_resolved)
        except (FileNotFoundError, OSError, ValueError):
            pass
        if resolved == protected_resolved or same_physical_file:
            raise ScanError("待复核草稿输出不得替换已接受输入")
    if not resolved.parent.is_dir():
        raise ScanError("待复核草稿输出的父目录不存在")
    return resolved


def atomic_write_json(
    path: Path,
    payload: dict[str, Any],
    before_replace: Callable[[], None] | None = None,
) -> None:
    descriptor = -1
    directory_descriptor = -1
    temp_name: str | None = None
    try:
        directory_flags = os.O_RDONLY
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        directory_stat = os.fstat(directory_descriptor)
        if not stat_module.S_ISDIR(directory_stat.st_mode):
            raise ScanError("待复核草稿输出的父路径必须是目录")

        visible_parent_stat = path.parent.lstat()
        if stat_module.S_ISLNK(visible_parent_stat.st_mode) or (
            visible_parent_stat.st_dev,
            visible_parent_stat.st_ino,
        ) != (directory_stat.st_dev, directory_stat.st_ino):
            raise ScanError("待复核草稿输出的父目录在校验后发生变化")

        for _ in range(128):
            candidate = f".get-index-draft-{secrets.token_hex(12)}.tmp"
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            create_flags |= getattr(os, "O_NOFOLLOW", 0)
            create_flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(
                    candidate,
                    create_flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if descriptor < 0 or temp_name is None:
            raise ScanError("待复核草稿无法分配临时文件")

        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        try:
            destination_stat = os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and (
            stat_module.S_ISLNK(destination_stat.st_mode)
            or not stat_module.S_ISREG(destination_stat.st_mode)
            or destination_stat.st_nlink != 1
        ):
            raise ScanError("待复核草稿输出必须保持为单硬链接普通文件")
        visible_parent_stat = path.parent.lstat()
        if stat_module.S_ISLNK(visible_parent_stat.st_mode) or (
            visible_parent_stat.st_dev,
            visible_parent_stat.st_ino,
        ) != (directory_stat.st_dev, directory_stat.st_ino):
            raise ScanError("待复核草稿输出的父目录在替换前发生变化")
        os.replace(
            temp_name,
            path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temp_name = None
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise ScanError(f"无法写入待复核草稿（{exc.__class__.__name__}）") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp_name is not None and directory_descriptor >= 0:
            try:
                os.unlink(temp_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def generate_review_draft(args: argparse.Namespace) -> dict[str, Any]:
    accepted = load_snapshot(args.snapshot)
    current = scan_current_sources(args.vault_root)
    report = load_scan_report(args.scan_report)
    records = load_canonical_records(args.records)
    validate_records_match_snapshot(records, accepted)
    bundle = validate_review_bundle(
        build_review_bundle(accepted, current, report, records)
    )
    output = resolved_output_path(
        args.output,
        args.vault_root,
        [args.snapshot, args.scan_report, *args.records],
    )

    def ensure_sources_still_match() -> None:
        final_current = scan_current_sources(args.vault_root)
        if source_fingerprint(final_current) != bundle["current_fingerprint"]:
            raise ScanError("生成待复核草稿期间 GET 来源发生变化")

    atomic_write_json(output, bundle, before_replace=ensure_sources_still_match)
    return {
        "schema_version": 1,
        "status": bundle["status"],
        "draft_count": len(bundle["drafts"]),
        "notice_count": len(bundle["notices"]),
        "wrote_review_draft": True,
    }


def check_review_draft(path: Path) -> dict[str, Any]:
    bundle = load_review_bundle(path)
    return {
        "schema_version": 1,
        "status": "valid_review_draft",
        "draft_count": len(bundle["drafts"]),
        "notice_count": len(bundle["notices"]),
        "unresolved_field_count": sum(
            len(draft["unresolved_fields"]) for draft in bundle["drafts"]
        ),
    }


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def canonical_directory_path(path: Path, label: str) -> Path:
    try:
        resolved = absolute_path(path).resolve(strict=True)
        directory_stat = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise ScanError(f"{label}无法安全解析") from exc
    if stat_module.S_ISLNK(directory_stat.st_mode) or not stat_module.S_ISDIR(
        directory_stat.st_mode
    ):
        raise ScanError(f"{label}必须是真实目录")
    return resolved


def canonical_regular_file_path(path: Path, label: str) -> Path:
    absolute = absolute_path(path)
    try:
        visible = absolute.lstat()
    except OSError as exc:
        raise ScanError(f"{label}不存在或无法检查") from exc
    if (
        stat_module.S_ISLNK(visible.st_mode)
        or not stat_module.S_ISREG(visible.st_mode)
        or visible.st_nlink != 1
    ):
        raise ScanError(f"{label}必须是单硬链接普通文件")
    try:
        canonical = absolute.parent.resolve(strict=True) / absolute.name
        canonical_stat = canonical.lstat()
    except (OSError, RuntimeError) as exc:
        raise ScanError(f"{label}的物理父目录无法安全解析") from exc
    if (
        stat_module.S_ISLNK(canonical_stat.st_mode)
        or not stat_module.S_ISREG(canonical_stat.st_mode)
        or canonical_stat.st_nlink != 1
        or (canonical_stat.st_dev, canonical_stat.st_ino)
        != (visible.st_dev, visible.st_ino)
    ):
        raise ScanError(f"{label}在路径规范化期间发生变化")
    return canonical


def path_is_inside(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    if resolved_path == resolved_root or resolved_root in resolved_path.parents:
        return True
    for candidate in (resolved_path, *resolved_path.parents):
        try:
            if os.path.samefile(candidate, resolved_root):
                return True
        except (FileNotFoundError, OSError, ValueError):
            continue
    return False


def validate_index_directory_location(vault_root: Path, index_directory: Path) -> None:
    try:
        if not path_is_inside(index_directory, vault_root):
            raise ScanError("固定覆盖索引目录的物理位置必须位于 Vault 内")
        get_sources = vault_root / "GET NOTES"
        if path_is_inside(index_directory, get_sources):
            raise ScanError("固定覆盖索引目录不得位于只读 GET NOTES 来源层")
    except (OSError, RuntimeError) as exc:
        raise ScanError("固定覆盖索引目录无法安全解析") from exc


def validate_external_apply_inputs(
    vault_root: Path,
    external_inputs: list[Path],
) -> None:
    try:
        if any(path_is_inside(path, vault_root) for path in external_inputs):
            raise ScanError("增量快照、待复核草稿和规范记录必须位于 Vault 外")
    except (OSError, RuntimeError) as exc:
        raise ScanError("apply 输入路径无法安全解析") from exc
    for index, path in enumerate(external_inputs):
        for other in external_inputs[index + 1 :]:
            try:
                if os.path.samefile(path, other):
                    raise ScanError("apply 输入文件不能互为物理别名")
            except FileNotFoundError as exc:
                raise ScanError("apply 输入文件在校验期间消失") from exc
            except OSError as exc:
                raise ScanError("apply 输入文件无法检查物理身份") from exc


def normalize_apply_arguments(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, list[Path], list[Path], Path]:
    if len(args.records) != 3:
        raise ScanError("apply 必须接收三份规范记录")
    vault_root = canonical_directory_path(args.vault_root, "Vault 根目录")
    snapshot = canonical_regular_file_path(args.snapshot, "增量快照")
    review_draft = canonical_regular_file_path(args.review_draft, "待复核草稿")
    records = [
        canonical_regular_file_path(path, f"规范记录文件 {index}")
        for index, path in enumerate(args.records, start=1)
    ]
    if len(set(records)) != len(records):
        raise ScanError("三份规范记录路径必须互不相同")
    index_directory = vault_root / "系统" / "知识库索引"
    validate_index_directory_location(vault_root, index_directory)
    pages = [index_directory / filename for filename in INDEX_PAGE_FILENAMES]
    external_inputs = [snapshot, review_draft, *records]
    validate_external_apply_inputs(vault_root, external_inputs)
    journal = snapshot.parent / "apply-transaction.json"
    return vault_root, snapshot, review_draft, records, pages, journal


def normalize_recovery_arguments(
    args: argparse.Namespace,
) -> tuple[Path, Path, list[Path], list[Path], Path]:
    if len(args.records) != 3:
        raise ScanError("recover-only 必须接收三份规范记录")
    vault_root = canonical_directory_path(args.vault_root, "Vault 根目录")
    snapshot = canonical_regular_file_path(args.snapshot, "增量快照")
    records = [
        canonical_regular_file_path(path, f"规范记录文件 {index}")
        for index, path in enumerate(args.records, start=1)
    ]
    if len(set(records)) != len(records):
        raise ScanError("三份规范记录路径必须互不相同")
    index_directory = vault_root / "系统" / "知识库索引"
    validate_index_directory_location(vault_root, index_directory)
    pages = [index_directory / filename for filename in INDEX_PAGE_FILENAMES]
    validate_external_apply_inputs(vault_root, [snapshot, *records])
    journal = snapshot.parent / "apply-transaction.json"
    return vault_root, snapshot, records, pages, journal


def validate_bundle_matches_delta(
    bundle: dict[str, Any],
    accepted: dict[str, dict[str, str]],
    current: dict[str, dict[str, str]],
) -> dict[str, Any]:
    report = compare_sources(accepted, current)
    changes_by_key = {
        (change["kind"], change["relative_path"]): change
        for change in report["changes"]
    }
    if any(kind == "missing" for kind, _ in changes_by_key):
        raise ScanError("当前变化包含缺失的 GET 来源，禁止应用")
    draft_by_key = {
        (draft["change_kind"], draft["relative_path"]): draft
        for draft in bundle["drafts"]
    }
    notice_by_key = {
        (notice["kind"], notice["relative_path"]): notice
        for notice in bundle["notices"]
    }
    expected_draft_keys = {
        key for key in changes_by_key if key[0] in {"added", "index_metadata_changed"}
    }
    expected_notice_keys = {
        key for key in changes_by_key if key[0] == "content_only_changed"
    }
    if set(draft_by_key) != expected_draft_keys or set(notice_by_key) != expected_notice_keys:
        raise ScanError("待复核草稿未完整且唯一地覆盖当前 GET 来源变化")
    for key, draft in draft_by_key.items():
        expected_facts = review_draft_facts(current[key[1]], key[0])
        if any(draft[field] != value for field, value in expected_facts.items()):
            raise ScanError("待复核草稿中的 GET 来源事实与当前来源不一致")
        if (
            draft["unresolved_fields"]
            or draft["domain"] is None
            or not 1 <= len(draft["tags"]) <= 2
            or draft["flag"] is None
            or draft["review_state"] == "待复核"
        ):
            raise ScanError("待复核草稿尚未完成人工复核，禁止应用")
    for key, notice in notice_by_key.items():
        expected_notice = {
            **changes_by_key[key],
            "action": "keep_accepted_governance",
        }
        if notice != expected_notice:
            raise ScanError("仅内容变化通知与当前 GET 来源不一致")
    return report


def merge_reviewed_records(
    parts: list[list[dict[str, Any]]],
    part_by_path: dict[str, int],
    bundle: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[list[dict[str, Any]]]]:
    merged_parts = [[dict(record) for record in part] for part in parts]
    for draft in bundle["drafts"]:
        canonical = {key: draft[key] for key in CANONICAL_KEYS}
        relative_path = canonical["relative_path"]
        if draft["change_kind"] == "added":
            merged_parts[-1].append(canonical)
        else:
            part_index = part_by_path[relative_path]
            row_index = next(
                index
                for index, record in enumerate(merged_parts[part_index])
                if record["relative_path"] == relative_path
            )
            merged_parts[part_index][row_index] = canonical
    for part in merged_parts:
        part.sort(key=lambda row: (row["created_date"], row["uid"]), reverse=True)
    merged = {
        record["relative_path"]: record
        for part in merged_parts
        for record in part
    }
    if len(merged) != sum(len(part) for part in merged_parts):
        raise ScanError("合并后的规范记录包含重复路径")
    uids = [record["uid"] for record in merged.values()]
    if len(set(uids)) != len(uids):
        raise ScanError("合并后的规范记录包含重复 UID")
    return merged, merged_parts


def review_bundle_is_already_applied(
    bundle: dict[str, Any],
    accepted: dict[str, dict[str, str]],
    current: dict[str, dict[str, str]],
    records: dict[str, dict[str, Any]],
) -> bool:
    if not bundle["drafts"] and not bundle["notices"]:
        return False
    accepted_fingerprint = source_fingerprint(accepted)
    if (
        bundle["accepted_fingerprint"] == accepted_fingerprint
        or bundle["current_fingerprint"] != accepted_fingerprint
        or source_fingerprint(current) != accepted_fingerprint
    ):
        return False
    for draft in bundle["drafts"]:
        relative_path = draft["relative_path"]
        if relative_path not in current or relative_path not in records:
            return False
        expected_facts = review_draft_facts(
            current[relative_path],
            draft["change_kind"],
        )
        if any(draft[key] != value for key, value in expected_facts.items()):
            return False
        if records[relative_path] != {
            key: draft[key]
            for key in CANONICAL_KEYS
        }:
            return False
    for notice in bundle["notices"]:
        relative_path = notice["relative_path"]
        if relative_path not in current:
            return False
        source = current[relative_path]
        if notice != {
            "kind": "content_only_changed",
            "relative_path": relative_path,
            "uid": source["uid"],
            "note_type": source["note_type"],
            "action": "keep_accepted_governance",
        }:
            return False
    return True


def serialize_record_part(rows: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False) + "\n"
        for row in rows
    ).encode("utf-8")


def serialize_snapshot(sources: dict[str, dict[str, str]]) -> bytes:
    payload = {
        "schema_version": 1,
        "sources": [sources[path] for path in sorted(sources)],
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def verify_persisted_index_pages(
    page_paths: list[Path],
    records: dict[str, dict[str, Any]],
    mismatch_message: str,
) -> None:
    rendered_pages = render_index_pages(list(records.values()))
    validate_index_pages(rendered_pages, list(records.values()))
    for page_path in page_paths:
        if read_regular_text(page_path, "覆盖索引页") != rendered_pages[page_path.name]:
            raise ScanError(mismatch_message)


@dataclass(frozen=True)
class PreparedReviewApplication:
    outcome: str
    current: dict[str, dict[str, str]]
    candidate_contents: dict[Path, bytes]
    expected_old_sha256: dict[Path, str]
    before_commit: Callable[[], None] | None
    changed_file_count: int


def prepare_review_application(
    vault_root: Path,
    snapshot: Path,
    review_draft: Path,
    record_paths: list[Path],
    page_paths: list[Path],
) -> PreparedReviewApplication:
    accepted, snapshot_bytes = load_snapshot_with_bytes(snapshot)
    current = scan_current_sources(vault_root)
    records, parts, part_by_path, record_bytes = load_canonical_record_parts(
        record_paths
    )
    validate_records_match_snapshot(records, accepted)
    bundle, review_bytes = load_review_bundle_with_bytes(review_draft)
    if review_bundle_is_already_applied(bundle, accepted, current, records):
        verify_persisted_index_pages(
            page_paths,
            records,
            "已应用复核结果的覆盖索引与规范记录不一致",
        )
        return PreparedReviewApplication(
            outcome="already_applied",
            current=current,
            candidate_contents={},
            expected_old_sha256={},
            before_commit=None,
            changed_file_count=0,
        )
    if bundle["accepted_fingerprint"] != source_fingerprint(accepted):
        raise ScanError("待复核草稿与已接受增量快照不一致")
    if bundle["current_fingerprint"] != source_fingerprint(current):
        raise ScanError("待复核草稿与当前 GET 来源不一致")
    if bundle["records_fingerprint"] != canonical_records_fingerprint(records):
        raise ScanError("待复核草稿与当前规范记录不一致")
    report = validate_bundle_matches_delta(bundle, accepted, current)
    if report["status"] == "unchanged":
        return PreparedReviewApplication(
            outcome="no_changes",
            current=current,
            candidate_contents={},
            expected_old_sha256={},
            before_commit=None,
            changed_file_count=0,
        )

    merged, merged_parts = merge_reviewed_records(parts, part_by_path, bundle)
    validate_records_match_snapshot(merged, current)
    rendered_pages = render_index_pages(list(merged.values()))
    validate_index_pages(rendered_pages, list(merged.values()))
    record_contents = {
        path: serialize_record_part(rows)
        for path, rows in zip(record_paths, merged_parts, strict=True)
    }
    page_contents = {
        path: rendered_pages[path.name].encode("utf-8")
        for path in page_paths
    }
    page_bytes = {
        path: read_regular_bytes(path, "覆盖索引页")
        for path in page_paths
    }
    snapshot_content = serialize_snapshot(current)
    candidate_contents = {
        **record_contents,
        **page_contents,
        snapshot: snapshot_content,
    }
    old_contents = {
        **record_bytes,
        **page_bytes,
        snapshot: snapshot_bytes,
    }
    expected_current_fingerprint = source_fingerprint(current)
    review_digest = hashlib.sha256(review_bytes).hexdigest()

    def verify_inputs_before_snapshot_commit() -> None:
        final_current = scan_current_sources(vault_root)
        if source_fingerprint(final_current) != expected_current_fingerprint:
            raise TransactionError("GET 来源在增量快照提交前发生变化")
        if hashlib.sha256(
            read_regular_bytes(review_draft, "待复核草稿")
        ).hexdigest() != review_digest:
            raise TransactionError("待复核草稿在增量快照提交前发生变化")
        for path, expected in {**record_contents, **page_contents}.items():
            if read_regular_bytes(path, "受控目标") != expected:
                raise TransactionError("受控目标在增量快照提交前与候选内容不一致")

    return PreparedReviewApplication(
        outcome="ready_to_apply",
        current=current,
        candidate_contents=candidate_contents,
        expected_old_sha256={
            path: hashlib.sha256(content).hexdigest()
            for path, content in old_contents.items()
        },
        before_commit=verify_inputs_before_snapshot_commit,
        changed_file_count=sum(
            old_contents[path] != content
            for path, content in candidate_contents.items()
        ),
    )


def apply_lock_directories(
    vault_root: Path,
    snapshot: Path,
    review_draft: Path,
    record_paths: list[Path],
    page_paths: list[Path],
) -> list[Path]:
    return [
        vault_root,
        vault_root / "系统",
        snapshot.parent,
        review_draft.parent,
        *(path.parent for path in record_paths),
        *(path.parent for path in page_paths),
    ]


def validate_locked_apply_paths(
    vault_root: Path,
    snapshot: Path,
    review_draft: Path,
    record_paths: list[Path],
    page_paths: list[Path],
) -> None:
    validate_index_directory_location(vault_root, page_paths[0].parent)
    validate_external_apply_inputs(
        vault_root,
        [snapshot, review_draft, *record_paths],
    )


def recovery_lock_directories(
    vault_root: Path,
    snapshot: Path,
    record_paths: list[Path],
    page_paths: list[Path],
) -> list[Path]:
    return [
        vault_root,
        vault_root / "系统",
        snapshot.parent,
        *(path.parent for path in record_paths),
        *(path.parent for path in page_paths),
    ]


def validate_locked_recovery_paths(
    vault_root: Path,
    snapshot: Path,
    record_paths: list[Path],
    page_paths: list[Path],
) -> None:
    validate_index_directory_location(vault_root, page_paths[0].parent)
    validate_external_apply_inputs(vault_root, [snapshot, *record_paths])


def require_no_pending_transaction(journal: Path) -> None:
    try:
        journal.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TransactionError("遗留应用事务日志无法检查") from exc
    raise TransactionError("检测到遗留应用事务；请先执行 recover-only 恢复")


def preflight_review_draft(args: argparse.Namespace) -> dict[str, Any]:
    vault_root, snapshot, review_draft, record_paths, page_paths, journal = (
        normalize_apply_arguments(args)
    )
    lock_directories = apply_lock_directories(
        vault_root,
        snapshot,
        review_draft,
        record_paths,
        page_paths,
    )
    with transaction_lock(*lock_directories):
        validate_locked_apply_paths(
            vault_root,
            snapshot,
            review_draft,
            record_paths,
            page_paths,
        )
        require_no_pending_transaction(journal)
        prepared = prepare_review_application(
            vault_root,
            snapshot,
            review_draft,
            record_paths,
            page_paths,
        )
        if prepared.outcome == "already_applied":
            return {
                "schema_version": 1,
                "status": "no_changes",
                "already_applied": True,
                "changed_file_count": 0,
                "source_count": len(prepared.current),
                "message": "这份人工复核结果已经应用；无需再次应用。",
            }
        if prepared.outcome == "no_changes":
            return {
                "schema_version": 1,
                "status": "no_changes",
                "already_applied": False,
                "changed_file_count": 0,
                "source_count": len(prepared.current),
                "message": "GET 来源无增量；无需应用。",
            }
        return {
            "schema_version": 1,
            "status": "ready_to_apply",
            "managed_target_count": len(prepared.candidate_contents),
            "changed_file_count": prepared.changed_file_count,
            "source_count": len(prepared.current),
            "message": "已完成实时来源、复核结果、规范记录和全部覆盖索引候选校验；可以应用。",
        }


def changed_source_count(report: dict[str, Any]) -> int:
    return sum(
        count
        for kind, count in report["counts"].items()
        if kind != "unchanged"
    )


def inspect_committed_application(
    vault_root: Path,
    snapshot: Path,
    record_paths: list[Path],
    page_paths: list[Path],
) -> dict[str, int]:
    accepted = load_snapshot(snapshot)
    current = scan_current_sources(vault_root)
    records, _, _, _ = load_canonical_record_parts(record_paths)
    validate_records_match_snapshot(records, accepted)
    verify_persisted_index_pages(
        page_paths,
        records,
        "已提交事务恢复后覆盖索引与规范记录不一致",
    )
    return {
        "source_count": len(accepted),
        "current_source_count": len(current),
        "pending_change_count": changed_source_count(
            compare_sources(accepted, current)
        ),
    }


def recover_apply_transaction(args: argparse.Namespace) -> dict[str, Any]:
    vault_root, snapshot, record_paths, page_paths, journal = (
        normalize_recovery_arguments(args)
    )
    allowed_targets = {snapshot, *record_paths, *page_paths}
    lock_directories = recovery_lock_directories(
        vault_root,
        snapshot,
        record_paths,
        page_paths,
    )
    with transaction_lock(*lock_directories) as locked_directories:
        validate_locked_recovery_paths(
            vault_root,
            snapshot,
            record_paths,
            page_paths,
        )
        committed_state: dict[str, int] = {}

        def validate_committed() -> None:
            committed_state.update(
                inspect_committed_application(
                    vault_root,
                    snapshot,
                    record_paths,
                    page_paths,
                )
            )

        recovery = recover_transaction(
            journal,
            allowed_targets,
            snapshot,
            locked_directories.identities,
            validate_committed=validate_committed,
        )
        if recovery == "none":
            return {
                "schema_version": 1,
                "status": "no_transaction",
                "recovered_transaction": "none",
                "changed_file_count": 0,
                "message": "未发现需要恢复的遗留应用事务。",
            }

        if recovery == "committed":
            return {
                "schema_version": 1,
                "status": "applied_recovered",
                "changed_file_count": 0,
                **committed_state,
                "recovered_transaction": "committed",
                "message": "上次应用已完成提交；本次已验证结果并清理遗留事务。",
            }
        accepted = load_snapshot(snapshot)
        current = scan_current_sources(vault_root)
        records, _, _, _ = load_canonical_record_parts(record_paths)
        validate_records_match_snapshot(records, accepted)
        pending_change_count = changed_source_count(compare_sources(accepted, current))
        return {
            "schema_version": 1,
            "status": "rolled_back",
            "changed_file_count": 0,
            "source_count": len(accepted),
            "current_source_count": len(current),
            "pending_change_count": pending_change_count,
            "recovered_transaction": "rolled_back",
            "message": "上次应用未提交，已恢复原状；来源变化仍待处理，需重新确认后再应用。",
        }


def apply_review_draft(args: argparse.Namespace) -> dict[str, Any]:
    vault_root, snapshot, review_draft, record_paths, page_paths, journal = (
        normalize_apply_arguments(args)
    )
    allowed_targets = {snapshot, *record_paths, *page_paths}
    lock_directories = apply_lock_directories(
        vault_root,
        snapshot,
        review_draft,
        record_paths,
        page_paths,
    )
    with transaction_lock(*lock_directories) as locked_directories:
        validate_locked_apply_paths(
            vault_root,
            snapshot,
            review_draft,
            record_paths,
            page_paths,
        )
        committed_state: dict[str, int] = {}

        def validate_committed() -> None:
            committed_state.update(
                inspect_committed_application(
                    vault_root,
                    snapshot,
                    record_paths,
                    page_paths,
                )
            )

        recovery = recover_transaction(
            journal,
            allowed_targets,
            snapshot,
            locked_directories.identities,
            validate_committed=validate_committed,
        )
        if recovery == "committed":
            return {
                "schema_version": 1,
                "status": "applied_recovered",
                "changed_file_count": 0,
                **committed_state,
                "recovered_transaction": recovery,
                "message": "上次应用已完成提交；本次已验证结果并清理遗留事务。",
            }
        prepared = prepare_review_application(
            vault_root,
            snapshot,
            review_draft,
            record_paths,
            page_paths,
        )
        if prepared.outcome == "already_applied":
            return {
                "schema_version": 1,
                "status": "no_changes",
                "already_applied": True,
                "changed_file_count": 0,
                "source_count": len(prepared.current),
                "recovered_transaction": recovery,
                "message": "这份人工复核结果已经应用；本次未重写任何受控文件。",
            }
        if prepared.outcome == "no_changes":
            return {
                "schema_version": 1,
                "status": "no_changes",
                "already_applied": False,
                "changed_file_count": 0,
                "source_count": len(prepared.current),
                "recovered_transaction": recovery,
                "message": "GET 来源无增量；未重写规范记录、覆盖索引或增量快照。",
            }
        changed_paths = replace_files_transactionally(
            prepared.candidate_contents,
            commit_marker=snapshot,
            journal_path=journal,
            before_commit=prepared.before_commit,
            expected_old_sha256=prepared.expected_old_sha256,
            expected_directory_identities=locked_directories.identities,
        )
        try:
            post_commit_current = scan_current_sources(vault_root)
            post_commit_report = compare_sources(prepared.current, post_commit_current)
        except ScanError:
            return {
                "schema_version": 1,
                "status": "applied_check_required",
                "snapshot_advanced": True,
                "changed_file_count": len(changed_paths),
                "source_count": len(prepared.current),
                "recovered_transaction": recovery,
                "message": "受控更新已提交，但提交后的 GET 来源复查失败；请重新运行检测。",
            }
        pending_change_count = sum(
            count
            for kind, count in post_commit_report["counts"].items()
            if kind != "unchanged"
        )
        if pending_change_count:
            return {
                "schema_version": 1,
                "status": "applied_with_new_changes",
                "snapshot_advanced": True,
                "changed_file_count": len(changed_paths),
                "source_count": len(prepared.current),
                "current_source_count": len(post_commit_current),
                "pending_change_count": pending_change_count,
                "recovered_transaction": recovery,
                "message": "本次受控更新已提交；提交点后又检测到新的 GET 变化，请重新运行检测。",
            }
        return {
            "schema_version": 1,
            "status": "applied",
            "snapshot_advanced": True,
            "changed_file_count": len(changed_paths),
            "source_count": len(prepared.current),
            "recovered_transaction": recovery,
            "message": "已更新规范记录与覆盖索引，并在最终校验后推进增量快照。",
        }


def error_report(message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "error",
        "errors": [line for line in message.splitlines() if line],
    }


def render_text(report: dict[str, Any]) -> str:
    if report["status"] == "error":
        lines = ["GET 增量检测失败："]
        lines.extend(f"- {error}" for error in report["errors"])
        return "\n".join(lines)
    counts = report["counts"]
    lines = [
        "GET 增量检测完成",
        (
            f"当前 GET 来源：{report['current_source_count']}，"
            f"已接受增量快照：{report['accepted_source_count']}"
        ),
        f"新增：{counts['added']}",
        f"索引元数据变化：{counts['index_metadata_changed']}",
        f"仅内容变化：{counts['content_only_changed']}",
        f"缺失：{counts['missing']}",
        f"无变化：{counts['unchanged']}",
        (
            "结果：无变化；未写入 Vault，也未推进增量快照。"
            if report["status"] == "unchanged"
            else "结果：发现变化；下一步：待复核。未写入 Vault，也未推进增量快照。"
        ),
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="比较当前 GET Markdown 与已接受增量快照")
    scan.add_argument("--vault-root", required=True, type=Path)
    scan.add_argument("--snapshot", required=True, type=Path)
    scan.add_argument("--format", choices=("text", "json"), default="text")
    draft = subparsers.add_parser(
        "draft", help="依据冻结扫描报告生成保守待复核草稿"
    )
    draft.add_argument("--vault-root", required=True, type=Path)
    draft.add_argument("--snapshot", required=True, type=Path)
    draft.add_argument("--scan-report", required=True, type=Path)
    draft.add_argument("--records", required=True, nargs="+", type=Path)
    draft.add_argument("--output", required=True, type=Path)
    check_draft = subparsers.add_parser(
        "check-draft", help="校验待复核草稿且不修改文件"
    )
    check_draft.add_argument("--draft", required=True, type=Path)
    apply_command = subparsers.add_parser(
        "apply", help="校验并应用已完成人工复核的 GET 增量"
    )
    apply_command.add_argument("--vault-root", required=True, type=Path)
    apply_command.add_argument("--snapshot", required=True, type=Path)
    apply_command.add_argument("--review-draft", required=True, type=Path)
    apply_command.add_argument("--records", required=True, nargs="+", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"draft", "check-draft", "apply"}:
        try:
            if args.command == "draft":
                report = generate_review_draft(args)
            elif args.command == "check-draft":
                report = check_review_draft(args.draft)
            else:
                report = apply_review_draft(args)
            exit_code = 0
        except (ScanError, IndexRenderError, TransactionError) as exc:
            report = error_report(str(exc))
            exit_code = 2
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
        return exit_code

    try:
        accepted = load_snapshot(args.snapshot)
        current = scan_current_sources(args.vault_root)
        report = compare_sources(accepted, current)
        exit_code = 0
    except ScanError as exc:
        report = error_report(str(exc))
        exit_code = 2

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(render_text(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
