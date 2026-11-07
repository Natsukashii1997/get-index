from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import get_index_incremental as incremental

from tests.test_incremental_scan import (
    snapshot_entry,
    source_tags_sha256,
    source_text,
    write_snapshot,
    write_source,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "get_index_incremental.py"


def canonical_record(path: Path, vault: Path, *, uid: str, title: str) -> dict:
    return {
        "relative_path": path.relative_to(vault).as_posix(),
        "uid": uid,
        "title": title,
        "created_date": "2026-09-15",
        "note_type": "recorder_audio",
        "domain": "工作/团队",
        "sensitivity": "内部",
        "tags": ["项目与流程"],
        "review_state": "规则初分",
        "basis": "metadata",
        "flag": "none",
    }


def write_records(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


class ReviewDraftCliTests(unittest.TestCase):
    def run_scan(self, vault: Path, snapshot: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CLI),
                "scan",
                "--vault-root",
                str(vault),
                "--snapshot",
                str(snapshot),
                "--format",
                "json",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

    def run_draft(
        self,
        vault: Path,
        snapshot: Path,
        scan_report: Path,
        records: list[Path],
        output: Path,
        *,
        timeout: float = 5,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(CLI),
            "draft",
            "--vault-root",
            str(vault),
            "--snapshot",
            str(snapshot),
            "--scan-report",
            str(scan_report),
            "--records",
            *[str(path) for path in records],
            "--output",
            str(output),
        ]
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def run_check_draft(self, draft: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), "check-draft", "--draft", str(draft)],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

    def test_added_source_generates_one_conservative_review_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable_relative = "GET NOTES/Notes/2026-09-15/stable.md"
            stable = write_source(vault, stable_relative, uid="uid-stable", title="stable")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            secret_body = "PRIVATE-BODY-MUST-NOT-APPEAR"
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/new.md",
                uid="uid-new",
                title="new title",
                tags=("raw-get-tag",),
                body=secret_body,
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"
            protected_before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (snapshot, records, stable)
            }

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "review_required")
            self.assertEqual(summary["draft_count"], 1)
            bundle = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(bundle["status"], "review_required")
            self.assertEqual(len(bundle["drafts"]), 1)
            draft = bundle["drafts"][0]
            self.assertEqual(draft["change_kind"], "added")
            self.assertEqual(draft["relative_path"], "GET NOTES/Notes/2026-09-15/new.md")
            self.assertEqual(draft["uid"], "uid-new")
            self.assertEqual(draft["title"], "new title")
            self.assertEqual(draft["created_date"], "2026-09-15")
            self.assertEqual(draft["note_type"], "recorder_audio")
            self.assertEqual(
                draft["source_tags_sha256"],
                source_tags_sha256(("raw-get-tag",)),
            )
            self.assertIsNone(draft["domain"])
            self.assertEqual(draft["sensitivity"], "受限")
            self.assertEqual(draft["tags"], [])
            self.assertEqual(draft["review_state"], "待复核")
            self.assertEqual(draft["basis"], "metadata")
            self.assertIsNone(draft["flag"])
            self.assertEqual(draft["unresolved_fields"], ["domain", "tags", "flag"])
            self.assertNotIn(secret_body, output.read_text(encoding="utf-8"))
            self.assertNotIn("raw-get-tag", output.read_text(encoding="utf-8"))
            self.assertNotIn(secret_body, result.stdout)
            self.assertNotIn(secret_body, result.stderr)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            protected_after = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (snapshot, records, stable)
            }
            self.assertEqual(protected_after, protected_before)

    def test_added_source_strips_url_query_parameters_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/stable.md",
                uid="uid-stable",
                title="stable",
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            secret = "PRIVATE-QUERY-TOKEN"
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/new.md",
                uid="uid-new",
                title=f"reference HTTPS://example.com/page?xsec_token={secret}#section",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertEqual(result.returncode, 0, result.stderr)
            persisted = output.read_text(encoding="utf-8")
            self.assertNotIn(secret, persisted)
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)
            draft = json.loads(persisted)["drafts"][0]
            self.assertEqual(
                draft["title"],
                "reference https://example.com/page#section",
            )

    def test_query_stripping_handles_parentheses_without_leaking(self) -> None:
        secret = "PRIVATE-PARENTHESIZED-QUERY"
        for source_title, expected in (
            (
                f"https://example.com/a?token=({secret})",
                "https://example.com/a",
            ),
            (
                f"https://example.com/a(b)?token={secret}",
                "https://example.com/a(b)",
            ),
            (
                f"https://example.com/a?token[]={secret}",
                "https://example.com/a",
            ),
            (
                f"Http://example.com/a?token={secret}",
                "http://example.com/a",
            ),
        ):
            with self.subTest(source_title=source_title):
                sanitized = incremental.strip_url_query_parameters(source_title)
                self.assertEqual(sanitized, expected)
                self.assertNotIn(secret, sanitized)
                with self.assertRaises(incremental.ScanError):
                    incremental.reject_query_urls([source_title], "test input")

    def test_index_metadata_change_preserves_accepted_governance_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="old title")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(source, vault, uid="uid-a", title="old title")],
            )
            records = root / "records.jsonl"
            record = canonical_record(source, vault, uid="uid-a", title="old title")
            record["domain"] = "工作/个人"
            record["sensitivity"] = "普通"
            record["tags"] = ["AI与自动化", "项目与流程"]
            record["flag"] = "stale"
            write_records(records, [record])
            source.write_text(
                source_text(
                    uid="uid-a",
                    title="current title",
                    tags=("PRIVATE-RAW-TAG",),
                ),
                encoding="utf-8",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(bundle["drafts"]), 1)
            draft = bundle["drafts"][0]
            self.assertEqual(draft["change_kind"], "index_metadata_changed")
            self.assertEqual(draft["title"], "current title")
            self.assertEqual(
                draft["source_tags_sha256"],
                source_tags_sha256(("PRIVATE-RAW-TAG",)),
            )
            self.assertEqual(draft["domain"], "工作/个人")
            self.assertEqual(draft["sensitivity"], "普通")
            self.assertEqual(draft["tags"], ["AI与自动化", "项目与流程"])
            self.assertEqual(draft["review_state"], "待复核")
            self.assertEqual(draft["basis"], "metadata")
            self.assertEqual(draft["flag"], "stale")
            self.assertEqual(draft["unresolved_fields"], [])
            self.assertNotIn("PRIVATE-RAW-TAG", output.read_text(encoding="utf-8"))

    def test_content_only_change_keeps_governance_and_creates_only_a_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            records = root / "records.jsonl"
            write_records(records, [canonical_record(source, vault, uid="uid-a", title="A")])
            secret_body = "PRIVATE-CHANGED-BODY-MUST-NOT-APPEAR"
            source.write_text(
                source_text(uid="uid-a", title="A", body=secret_body),
                encoding="utf-8",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"
            protected_before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (snapshot, records)
            }

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(bundle["status"], "no_review_needed")
            self.assertEqual(bundle["drafts"], [])
            self.assertEqual(
                bundle["notices"],
                [
                    {
                        "kind": "content_only_changed",
                        "relative_path": relative,
                        "uid": "uid-a",
                        "note_type": "recorder_audio",
                        "action": "keep_accepted_governance",
                    }
                ],
            )
            self.assertNotIn(secret_body, output.read_text(encoding="utf-8"))
            protected_after = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (snapshot, records)
            }
            self.assertEqual(protected_after, protected_before)

    def test_missing_source_fails_without_replacing_an_existing_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            records = root / "records.jsonl"
            write_records(records, [canonical_record(source, vault, uid="uid-a", title="A")])
            source.unlink()
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"
            output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
            output_before = (output.read_bytes(), output.stat().st_mtime_ns)

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertNotEqual(result.returncode, 0)
            error = json.loads(result.stdout)
            self.assertEqual(error["status"], "error")
            self.assertTrue(any("缺失" in message for message in error["errors"]))
            self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), output_before)

    def test_stale_or_tampered_scan_report_preserves_existing_output(self) -> None:
        for case in ("stale_source", "tampered_report"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                stable_relative = "GET NOTES/Notes/2026-09-15/stable.md"
                stable = write_source(vault, stable_relative, uid="uid-stable", title="stable")
                snapshot = root / "accepted-snapshot.json"
                write_snapshot(
                    snapshot,
                    [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
                )
                records = root / "records.jsonl"
                write_records(
                    records,
                    [canonical_record(stable, vault, uid="uid-stable", title="stable")],
                )
                added = write_source(
                    vault,
                    "GET NOTES/Notes/2026-09-15/new.md",
                    uid="uid-new",
                    title="new",
                )
                scan_result = self.run_scan(vault, snapshot)
                self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
                scan_report = root / "scan-report.json"
                scan_payload = json.loads(scan_result.stdout)
                if case == "tampered_report":
                    scan_payload["counts"]["added"] = 99
                scan_report.write_text(json.dumps(scan_payload), encoding="utf-8")
                if case == "stale_source":
                    added.write_text(
                        source_text(
                            uid="uid-new",
                            title="changed after scan",
                            body="PRIVATE-POST-SCAN-BODY",
                        ),
                        encoding="utf-8",
                    )
                output = root / "review-draft.json"
                output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
                output_before = (output.read_bytes(), output.stat().st_mtime_ns)

                result = self.run_draft(vault, snapshot, scan_report, [records], output)

                self.assertNotEqual(result.returncode, 0)
                error = json.loads(result.stdout)
                self.assertEqual(error["status"], "error")
                self.assertNotIn("PRIVATE-POST-SCAN-BODY", result.stdout)
                self.assertNotIn("PRIVATE-POST-SCAN-BODY", result.stderr)
                self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), output_before)

    def test_invalid_canonical_records_fail_without_leaking_or_replacing_output(self) -> None:
        for case in (
            "missing_field",
            "invalid_enum",
            "extra_private_field",
            "unhashable_tag",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                stable_relative = "GET NOTES/Notes/2026-09-15/stable.md"
                stable = write_source(vault, stable_relative, uid="uid-stable", title="stable")
                snapshot = root / "accepted-snapshot.json"
                write_snapshot(
                    snapshot,
                    [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
                )
                record = canonical_record(stable, vault, uid="uid-stable", title="stable")
                if case == "missing_field":
                    record.pop("domain")
                elif case == "invalid_enum":
                    record["sensitivity"] = "PRIVATE-INVALID-SENSITIVITY"
                elif case == "extra_private_field":
                    record["summary"] = "PRIVATE-SUMMARY-MUST-NOT-LEAK"
                else:
                    record["tags"] = [{}]
                records = root / "records.jsonl"
                write_records(records, [record])
                write_source(
                    vault,
                    "GET NOTES/Notes/2026-09-15/new.md",
                    uid="uid-new",
                    title="new",
                )
                scan_result = self.run_scan(vault, snapshot)
                self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
                scan_report = root / "scan-report.json"
                scan_report.write_text(scan_result.stdout, encoding="utf-8")
                output = root / "review-draft.json"
                output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
                output_before = (output.read_bytes(), output.stat().st_mtime_ns)

                result = self.run_draft(vault, snapshot, scan_report, [records], output)

                self.assertNotEqual(result.returncode, 0)
                error = json.loads(result.stdout)
                self.assertEqual(error["status"], "error")
                self.assertTrue(
                    any("规范记录" in message for message in error["errors"]),
                    error["errors"],
                )
                self.assertNotIn("PRIVATE", result.stdout)
                self.assertNotIn("PRIVATE", result.stderr)
                self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), output_before)

    def test_canonical_records_must_match_the_accepted_snapshot(self) -> None:
        for case in ("missing_record", "fact_mismatch"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                first = write_source(
                    vault,
                    "GET NOTES/Notes/2026-09-15/first.md",
                    uid="uid-first",
                    title="first",
                )
                second = write_source(
                    vault,
                    "GET NOTES/Notes/2026-09-15/second.md",
                    uid="uid-second",
                    title="second",
                )
                snapshot = root / "accepted-snapshot.json"
                write_snapshot(
                    snapshot,
                    [
                        snapshot_entry(first, vault, uid="uid-first", title="first"),
                        snapshot_entry(second, vault, uid="uid-second", title="second"),
                    ],
                )
                canonical = [
                    canonical_record(first, vault, uid="uid-first", title="first"),
                    canonical_record(second, vault, uid="uid-second", title="second"),
                ]
                if case == "missing_record":
                    canonical.pop()
                else:
                    canonical[1]["title"] = "different accepted fact"
                records = root / "records.jsonl"
                write_records(records, canonical)
                scan_result = self.run_scan(vault, snapshot)
                self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
                scan_report = root / "scan-report.json"
                scan_report.write_text(scan_result.stdout, encoding="utf-8")
                output = root / "review-draft.json"
                output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
                protected_paths = (first, second, snapshot, records, scan_report, output)
                protected_before = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths
                }

                result = self.run_draft(vault, snapshot, scan_report, [records], output)

                self.assertNotEqual(result.returncode, 0)
                error = json.loads(result.stdout)
                self.assertEqual(error["status"], "error")
                protected_after = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths
                }
                self.assertEqual(protected_after, protected_before)

    def test_canonical_records_with_query_url_are_rejected_without_leaking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            secret = "PRIVATE-CANONICAL-QUERY"
            title = f"https://example.com/page?token={secret}"
            source = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/stable.md",
                uid="uid-stable",
                title=title,
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [
                    snapshot_entry(
                        source,
                        vault,
                        uid="uid-stable",
                        title="https://example.com/page",
                    )
                ],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(source, vault, uid="uid-stable", title=title)],
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"
            output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
            output_before = (output.read_bytes(), output.stat().st_mtime_ns)

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertNotEqual(result.returncode, 0)
            error = json.loads(result.stdout)
            self.assertEqual(error["status"], "error")
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)
            self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), output_before)

    def test_added_and_missing_in_one_batch_fails_without_partial_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            kept = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/kept.md",
                uid="uid-kept",
                title="kept",
            )
            removed = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/removed.md",
                uid="uid-removed",
                title="removed",
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [
                    snapshot_entry(kept, vault, uid="uid-kept", title="kept"),
                    snapshot_entry(removed, vault, uid="uid-removed", title="removed"),
                ],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [
                    canonical_record(kept, vault, uid="uid-kept", title="kept"),
                    canonical_record(removed, vault, uid="uid-removed", title="removed"),
                ],
            )
            removed.unlink()
            added = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/added.md",
                uid="uid-added",
                title="added",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"
            output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
            protected_paths = (kept, added, snapshot, records, scan_report, output)
            protected_before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths
            }

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertNotEqual(result.returncode, 0)
            error = json.loads(result.stdout)
            self.assertEqual(error["status"], "error")
            self.assertTrue(any("缺失" in message for message in error["errors"]))
            protected_after = {
                path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths
            }
            self.assertEqual(protected_after, protected_before)

    def test_source_change_during_draft_build_preserves_every_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/stable.md",
                uid="uid-stable",
                title="stable",
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            added = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/added.md",
                uid="uid-added",
                title="added",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"
            output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
            current = incremental.scan_current_sources(vault)
            changed = copy.deepcopy(current)
            changed[added.relative_to(vault).as_posix()]["sha256"] = "0" * 64
            protected_paths = (stable, added, snapshot, records, scan_report, output)
            protected_before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths
            }
            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                scan_report=scan_report,
                records=[records],
                output=output,
            )

            with mock.patch.object(
                incremental,
                "scan_current_sources",
                side_effect=[current, changed],
            ):
                with self.assertRaisesRegex(incremental.ScanError, "GET 来源发生变化"):
                    incremental.generate_review_draft(args)

            protected_after = {
                path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths
            }
            self.assertEqual(protected_after, protected_before)

    def test_source_change_after_bundle_build_is_caught_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/stable.md",
                uid="uid-stable",
                title="stable",
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            added = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/added.md",
                uid="uid-added",
                title="added",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"
            output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
            output_before = (output.read_bytes(), output.stat().st_mtime_ns)
            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                scan_report=scan_report,
                records=[records],
                output=output,
            )
            real_resolver = incremental.resolved_output_path
            secret = "PRIVATE-POST-BUNDLE-BODY"

            def change_source_after_resolving(*resolver_args, **resolver_kwargs):
                resolved = real_resolver(*resolver_args, **resolver_kwargs)
                added.write_text(
                    source_text(uid="uid-added", title="changed", body=secret),
                    encoding="utf-8",
                )
                return resolved

            with mock.patch.object(
                incremental,
                "resolved_output_path",
                side_effect=change_source_after_resolving,
            ):
                with self.assertRaisesRegex(incremental.ScanError, "GET 来源发生变化"):
                    incremental.generate_review_draft(args)

            self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), output_before)
            self.assertNotIn(secret, output.read_text(encoding="utf-8"))
            self.assertEqual(list(root.glob(".get-index-draft-*.tmp")), [])

    def test_output_parent_symlink_swap_cannot_redirect_write_into_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/stable.md",
                uid="uid-stable",
                title="stable",
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output_dir = root / "output"
            output_dir.mkdir()
            moved_output_dir = root / "output-before-swap"
            output = output_dir / "review-draft.json"
            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                scan_report=scan_report,
                records=[records],
                output=output,
            )
            real_resolver = incremental.resolved_output_path

            def swap_parent_after_resolving(*resolver_args, **resolver_kwargs):
                resolved = real_resolver(*resolver_args, **resolver_kwargs)
                output_dir.rename(moved_output_dir)
                output_dir.symlink_to(vault, target_is_directory=True)
                return resolved

            with mock.patch.object(
                incremental,
                "resolved_output_path",
                side_effect=swap_parent_after_resolving,
            ):
                with self.assertRaises(incremental.ScanError):
                    incremental.generate_review_draft(args)

            self.assertFalse((vault / "review-draft.json").exists())
            self.assertEqual(list(vault.glob(".get-index-draft-*.tmp")), [])

    def test_atomic_replace_failure_preserves_old_output_and_removes_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/stable.md",
                uid="uid-stable",
                title="stable",
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            added = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/added.md",
                uid="uid-added",
                title="added",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"
            output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
            protected_paths = (stable, added, snapshot, records, scan_report, output)
            protected_before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths
            }
            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                scan_report=scan_report,
                records=[records],
                output=output,
            )

            with mock.patch.object(
                incremental.os,
                "replace",
                side_effect=OSError("synthetic replace failure"),
            ):
                with self.assertRaisesRegex(incremental.ScanError, "无法写入待复核草稿"):
                    incremental.generate_review_draft(args)

            protected_after = {
                path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected_paths
            }
            self.assertEqual(protected_after, protected_before)
            self.assertEqual(list(root.glob(".get-index-draft-*.tmp")), [])

    def test_symlink_output_is_rejected_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable_relative = "GET NOTES/Notes/2026-09-15/stable.md"
            stable = write_source(vault, stable_relative, uid="uid-stable", title="stable")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/new.md",
                uid="uid-new",
                title="new",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            target = root / "must-not-change.json"
            target.write_text("KEEP-SYMLINK-TARGET", encoding="utf-8")
            target_before = (target.read_bytes(), target.stat().st_mtime_ns)
            output = root / "review-draft.json"
            output.symlink_to(target)

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertNotEqual(result.returncode, 0)
            error = json.loads(result.stdout)
            self.assertEqual(error["status"], "error")
            self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), target_before)
            self.assertTrue(output.is_symlink())

    def test_hardlinked_output_is_rejected_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable_relative = "GET NOTES/Notes/2026-09-15/stable.md"
            stable = write_source(vault, stable_relative, uid="uid-stable", title="stable")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            target = root / "must-not-change.json"
            target.write_text("KEEP-HARDLINK-TARGET", encoding="utf-8")
            output = root / "review-draft.json"
            os.link(target, output)
            target_before = (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino)
            output_before = (output.read_bytes(), output.stat().st_mtime_ns, output.stat().st_ino)

            result = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertNotEqual(result.returncode, 0)
            error = json.loads(result.stdout)
            self.assertEqual(error["status"], "error")
            self.assertEqual(
                (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino),
                target_before,
            )
            self.assertEqual(
                (output.read_bytes(), output.stat().st_mtime_ns, output.stat().st_ino),
                output_before,
            )

    def test_output_cannot_replace_vault_snapshot_report_or_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable_relative = "GET NOTES/Notes/2026-09-15/stable.md"
            stable = write_source(vault, stable_relative, uid="uid-stable", title="stable")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")

            for label, output in (
                ("vault", vault / "review-draft.json"),
                ("snapshot", snapshot),
                ("scan_report", scan_report),
                ("records", records),
            ):
                with self.subTest(label=label):
                    protected_before = {
                        path: (path.read_bytes(), path.stat().st_mtime_ns)
                        for path in (stable, snapshot, scan_report, records)
                    }

                    result = self.run_draft(vault, snapshot, scan_report, [records], output)

                    self.assertNotEqual(result.returncode, 0)
                    error = json.loads(result.stdout)
                    self.assertEqual(error["status"], "error")
                    protected_after = {
                        path: (path.read_bytes(), path.stat().st_mtime_ns)
                        for path in (stable, snapshot, scan_report, records)
                    }
                    self.assertEqual(protected_after, protected_before)
                    if label == "vault":
                        self.assertFalse(output.exists())

    def test_physical_alias_cannot_bypass_vault_or_protected_input_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            alias_vault = root / "physical-alias-vault"
            alias_vault.mkdir()
            protected = root / "accepted-snapshot.json"
            protected.write_text("protected", encoding="utf-8")
            alias_protected = root / "physical-alias-snapshot.json"
            alias_protected.write_text("alias", encoding="utf-8")
            real_samefile = os.path.samefile

            def samefile_with_aliases(first_path, second_path):
                first = Path(first_path).resolve(strict=False)
                second = Path(second_path).resolve(strict=False)
                pair = frozenset((first, second))
                if pair in {
                    frozenset((alias_vault.resolve(), vault.resolve())),
                    frozenset((alias_protected.resolve(), protected.resolve())),
                }:
                    return True
                return real_samefile(first, second)

            with mock.patch.object(
                incremental.os.path,
                "samefile",
                side_effect=samefile_with_aliases,
            ):
                with self.assertRaises(incremental.ScanError):
                    incremental.resolved_output_path(
                        alias_vault / "review-draft.json",
                        vault,
                        [],
                    )
                with self.assertRaises(incremental.ScanError):
                    incremental.resolved_output_path(
                        alias_protected,
                        vault,
                        [protected],
                    )

    def test_snapshot_symlink_and_fifo_are_rejected_without_blocking(self) -> None:
        for case in ("symlink", "fifo"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                stable = write_source(
                    vault,
                    "GET NOTES/Notes/2026-09-15/stable.md",
                    uid="uid-stable",
                    title="stable",
                )
                real_snapshot = root / "accepted-snapshot.json"
                write_snapshot(
                    real_snapshot,
                    [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
                )
                records = root / "records.jsonl"
                write_records(
                    records,
                    [canonical_record(stable, vault, uid="uid-stable", title="stable")],
                )
                scan_result = self.run_scan(vault, real_snapshot)
                self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
                scan_report = root / "scan-report.json"
                scan_report.write_text(scan_result.stdout, encoding="utf-8")
                unsafe_snapshot = root / f"unsafe-{case}-snapshot.json"
                if case == "symlink":
                    unsafe_snapshot.symlink_to(real_snapshot)
                else:
                    os.mkfifo(unsafe_snapshot)
                output = root / "review-draft.json"
                output.write_text("KEEP-EXISTING-DRAFT", encoding="utf-8")
                output_before = (output.read_bytes(), output.stat().st_mtime_ns)

                result = self.run_draft(
                    vault,
                    unsafe_snapshot,
                    scan_report,
                    [records],
                    output,
                    timeout=1,
                )

                self.assertNotEqual(result.returncode, 0)
                error = json.loads(result.stdout)
                self.assertEqual(error["status"], "error")
                self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), output_before)

    def test_no_change_draft_is_empty_and_byte_stable_on_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/stable.md"
            source = write_source(vault, relative, uid="uid-stable", title="stable")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(source, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(source, vault, uid="uid-stable", title="stable")],
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"

            first = self.run_draft(vault, snapshot, scan_report, [records], output)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_bytes = output.read_bytes()
            bundle = json.loads(first_bytes)
            scan_payload = json.loads(scan_report.read_text(encoding="utf-8"))
            self.assertEqual(bundle["status"], "no_review_needed")
            self.assertEqual(bundle["drafts"], [])
            self.assertEqual(bundle["notices"], [])
            self.assertEqual(
                bundle["accepted_fingerprint"], scan_payload["accepted_fingerprint"]
            )
            self.assertEqual(bundle["current_fingerprint"], scan_payload["current_fingerprint"])
            self.assertEqual(
                bundle["records_fingerprint"],
                "sha256:665ed50ca6152fe2a2bdfcfda977f9b1917e79070662e2d61a3d73fd78e59cb0",
            )

            second = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(output.read_bytes(), first_bytes)
            self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))

    def test_mixed_changes_generate_sorted_byte_stable_review_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            metadata_relative = "GET NOTES/Notes/2026-09-15/b-metadata.md"
            content_relative = "GET NOTES/Notes/2026-09-15/c-content.md"
            metadata_source = write_source(
                vault, metadata_relative, uid="uid-metadata", title="old title"
            )
            content_source = write_source(
                vault, content_relative, uid="uid-content", title="content"
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [
                    snapshot_entry(
                        metadata_source,
                        vault,
                        uid="uid-metadata",
                        title="old title",
                    ),
                    snapshot_entry(
                        content_source,
                        vault,
                        uid="uid-content",
                        title="content",
                    ),
                ],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [
                    canonical_record(
                        metadata_source,
                        vault,
                        uid="uid-metadata",
                        title="old title",
                    ),
                    canonical_record(
                        content_source,
                        vault,
                        uid="uid-content",
                        title="content",
                    ),
                ],
            )
            metadata_source.write_text(
                source_text(uid="uid-metadata", title="new title"),
                encoding="utf-8",
            )
            content_source.write_text(
                source_text(uid="uid-content", title="content", body="changed body"),
                encoding="utf-8",
            )
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/a-added.md",
                uid="uid-added",
                title="added",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            output = root / "review-draft.json"

            first = self.run_draft(vault, snapshot, scan_report, [records], output)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_bytes = output.read_bytes()
            bundle = json.loads(first_bytes)
            self.assertEqual(
                [draft["change_kind"] for draft in bundle["drafts"]],
                ["added", "index_metadata_changed"],
            )
            self.assertEqual(
                [draft["relative_path"] for draft in bundle["drafts"]],
                [
                    "GET NOTES/Notes/2026-09-15/a-added.md",
                    metadata_relative,
                ],
            )
            self.assertEqual(len(bundle["notices"]), 1)
            self.assertEqual(bundle["notices"][0]["relative_path"], content_relative)

            second = self.run_draft(vault, snapshot, scan_report, [records], output)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(output.read_bytes(), first_bytes)
            self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))

    def test_check_draft_rejects_missing_uncontrolled_or_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable_relative = "GET NOTES/Notes/2026-09-15/stable.md"
            stable = write_source(vault, stable_relative, uid="uid-stable", title="stable")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(
                snapshot,
                [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
            )
            records = root / "records.jsonl"
            write_records(
                records,
                [canonical_record(stable, vault, uid="uid-stable", title="stable")],
            )
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/new.md",
                uid="uid-new",
                title="new",
            )
            scan_result = self.run_scan(vault, snapshot)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            scan_report = root / "scan-report.json"
            scan_report.write_text(scan_result.stdout, encoding="utf-8")
            valid_draft = root / "valid-review-draft.json"
            result = self.run_draft(vault, snapshot, scan_report, [records], valid_draft)
            self.assertEqual(result.returncode, 0, result.stderr)

            valid_result = self.run_check_draft(valid_draft)
            self.assertEqual(valid_result.returncode, 0, valid_result.stderr)
            self.assertEqual(json.loads(valid_result.stdout)["status"], "valid_review_draft")

            valid_payload = json.loads(valid_draft.read_text(encoding="utf-8"))
            for case in (
                "missing_field",
                "invalid_enum",
                "extra_private_field",
                "query_url",
                "unhashable_change_kind",
                "unhashable_domain",
                "unhashable_sensitivity",
                "unhashable_review_state",
                "unhashable_basis",
                "unhashable_flag",
            ):
                with self.subTest(case=case):
                    payload = copy.deepcopy(valid_payload)
                    if case == "missing_field":
                        payload["drafts"][0].pop("domain")
                    elif case == "invalid_enum":
                        payload["drafts"][0]["domain"] = "PRIVATE-INVALID-DOMAIN"
                    elif case == "extra_private_field":
                        payload["drafts"][0]["body"] = "PRIVATE-BODY-MUST-NOT-LEAK"
                    elif case == "query_url":
                        payload["drafts"][0]["title"] = (
                            "https://example.com/page?token=PRIVATE-QUERY-MUST-NOT-LEAK"
                        )
                    elif case == "unhashable_change_kind":
                        payload["drafts"][0]["change_kind"] = []
                    elif case == "unhashable_domain":
                        payload["drafts"][0]["domain"] = []
                    elif case == "unhashable_sensitivity":
                        payload["drafts"][0]["sensitivity"] = {}
                    elif case == "unhashable_review_state":
                        payload["drafts"][0]["review_state"] = []
                    elif case == "unhashable_basis":
                        payload["drafts"][0]["basis"] = {}
                    else:
                        payload["drafts"][0]["flag"] = []
                    candidate = root / f"{case}.json"
                    candidate.write_text(json.dumps(payload), encoding="utf-8")

                    invalid_result = self.run_check_draft(candidate)

                    self.assertNotEqual(invalid_result.returncode, 0)
                    error = json.loads(invalid_result.stdout)
                    self.assertEqual(error["status"], "error")
                    self.assertNotIn("PRIVATE", invalid_result.stdout)
                    self.assertNotIn("PRIVATE", invalid_result.stderr)

    def test_check_draft_rejects_duplicate_or_cross_colliding_notices(self) -> None:
        fingerprint = "sha256:" + "0" * 64
        draft = {
            "change_kind": "added",
            "relative_path": "GET NOTES/Notes/2026-09-15/new.md",
            "uid": "uid-new",
            "title": "new",
            "created_date": "2026-09-15",
            "note_type": "recorder_audio",
            "source_tags_sha256": "1" * 64,
            "domain": None,
            "sensitivity": "受限",
            "tags": [],
            "review_state": "待复核",
            "basis": "metadata",
            "flag": None,
            "unresolved_fields": ["domain", "tags", "flag"],
        }
        notice = {
            "kind": "content_only_changed",
            "relative_path": "GET NOTES/Notes/2026-09-15/stable.md",
            "uid": "uid-stable",
            "note_type": "recorder_audio",
            "action": "keep_accepted_governance",
        }
        cases = {
            "duplicate_notice": {
                "status": "no_review_needed",
                "drafts": [],
                "notices": [notice, copy.deepcopy(notice)],
            },
            "cross_collision": {
                "status": "review_required",
                "drafts": [draft],
                "notices": [
                    {
                        **notice,
                        "relative_path": draft["relative_path"],
                        "uid": draft["uid"],
                    }
                ],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for case, content in cases.items():
                with self.subTest(case=case):
                    payload = {
                        "schema_version": 1,
                        "status": content["status"],
                        "accepted_fingerprint": fingerprint,
                        "current_fingerprint": fingerprint,
                        "records_fingerprint": fingerprint,
                        "drafts": content["drafts"],
                        "notices": content["notices"],
                    }
                    candidate = root / f"{case}.json"
                    candidate.write_text(json.dumps(payload), encoding="utf-8")

                    result = self.run_check_draft(candidate)

                    self.assertNotEqual(result.returncode, 0)
                    error = json.loads(result.stdout)
                    self.assertEqual(error["status"], "error")


if __name__ == "__main__":
    unittest.main()
