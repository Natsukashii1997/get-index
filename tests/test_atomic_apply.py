from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import get_index_incremental as incremental
import get_index_transaction as transaction

from tests.test_incremental_scan import (
    snapshot_entry,
    source_text,
    write_snapshot,
    write_source,
)
from tests.test_review_drafts import canonical_record, write_records

from get_index_transaction import (
    TransactionError,
    recover_transaction,
    replace_files_transactionally,
    transaction_lock,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "get_index_incremental.py"
VALIDATOR = PROJECT_ROOT / "validate_get_index.py"
INDEX_FILENAMES = (
    "GET 来源索引｜总入口.md",
    "GET 来源索引｜生活.md",
    "GET 来源索引｜工作个人.md",
    "GET 来源索引｜工作团队.md",
    "GET 来源索引｜待复核.md",
)


class AtomicApplyCliTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def prepare_base_case(
        self, root: Path
    ) -> tuple[Path, Path, list[Path], list[Path]]:
        vault = root / "vault"
        source = write_source(
            vault,
            "GET NOTES/Notes/2026-09-15/stable.md",
            uid="uid-stable",
            title="stable",
        )
        snapshot = root / "state" / "accepted-snapshot.json"
        snapshot.parent.mkdir(parents=True)
        write_snapshot(
            snapshot,
            [snapshot_entry(source, vault, uid="uid-stable", title="stable")],
        )
        records = [root / name for name in ("part-1.jsonl", "part-2.jsonl", "part-3.jsonl")]
        write_records(
            records[0],
            [canonical_record(source, vault, uid="uid-stable", title="stable")],
        )
        write_records(records[1], [])
        write_records(records[2], [])

        index_dir = vault / "系统" / "知识库索引"
        index_dir.mkdir(parents=True)
        pages = [index_dir / filename for filename in INDEX_FILENAMES]
        for index, page in enumerate(pages, start=1):
            page.write_text(f"EXISTING-INDEX-{index}\n", encoding="utf-8")
        return vault, snapshot, records, pages

    def generate_review_draft(
        self,
        root: Path,
        vault: Path,
        snapshot: Path,
        records: list[Path],
    ) -> Path:
        scan = self.run_cli(
            "scan",
            "--vault-root",
            str(vault),
            "--snapshot",
            str(snapshot),
            "--format",
            "json",
        )
        self.assertEqual(scan.returncode, 0, scan.stderr)
        scan_report = root / "scan-report.json"
        scan_report.write_text(scan.stdout, encoding="utf-8")
        review_draft = root / "review-draft.json"
        draft = self.run_cli(
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
            str(review_draft),
        )
        self.assertEqual(draft.returncode, 0, draft.stderr)
        return review_draft

    def run_apply(
        self,
        vault: Path,
        snapshot: Path,
        review_draft: Path,
        records: list[Path],
    ) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "apply",
            "--vault-root",
            str(vault),
            "--snapshot",
            str(snapshot),
            "--review-draft",
            str(review_draft),
            "--records",
            *[str(path) for path in records],
        )

    def prepare_transaction_case(
        self,
        root: Path,
    ) -> tuple[Path, Path, dict[Path, bytes], dict[Path, tuple[bytes, int, int]]]:
        state = root / "state"
        records_dir = root / "records"
        index_dir = root / "vault" / "系统" / "知识库索引"
        for directory in (state, records_dir, index_dir):
            directory.mkdir(parents=True)
        record = records_dir / "part.jsonl"
        page = index_dir / "GET 来源索引｜总入口.md"
        snapshot = state / "accepted-snapshot.json"
        for path, content, mode, mtime in (
            (record, b"OLD-RECORD\n", 0o640, 1_700_000_001_000_000_000),
            (page, b"OLD-PAGE\n", 0o600, 1_700_000_002_000_000_000),
            (snapshot, b"OLD-SNAPSHOT\n", 0o644, 1_700_000_003_000_000_000),
        ):
            path.write_bytes(content)
            os.chmod(path, mode)
            os.utime(path, ns=(mtime, mtime))
        targets = {
            record: b"NEW-RECORD\n",
            page: b"NEW-PAGE\n",
            snapshot: b"NEW-SNAPSHOT\n",
        }
        before = {
            path: (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_mtime_ns)
            for path in targets
        }
        return state, snapshot, targets, before

    def test_apply_containment_recognizes_a_physical_vault_path_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            alias = root / "physical-alias"
            vault.mkdir()
            alias.mkdir()
            candidate = alias / "record.jsonl"
            candidate.write_text("{}\n", encoding="utf-8")

            real_samefile = os.path.samefile
            resolved_alias = alias.resolve()
            resolved_vault = vault.resolve()

            def samefile(left, right):
                if Path(left) == resolved_alias and Path(right) == resolved_vault:
                    return True
                return real_samefile(left, right)

            with mock.patch("get_index_incremental.os.path.samefile", side_effect=samefile):
                self.assertTrue(incremental.path_is_inside(candidate, vault))

    def test_apply_rejects_an_index_directory_reached_through_an_external_symlink(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, original_pages = self.prepare_base_case(root)
            for page in original_pages:
                page.unlink()
            original_pages[0].parent.rmdir()
            system_directory = vault / "系统"
            system_directory.rmdir()
            outside_system = root / "outside-system"
            outside_index = outside_system / "知识库索引"
            outside_index.mkdir(parents=True)
            outside_pages = [outside_index / filename for filename in INDEX_FILENAMES]
            for index, page in enumerate(outside_pages, start=1):
                page.write_text(f"OUTSIDE-INDEX-{index}\n", encoding="utf-8")
            system_directory.symlink_to(outside_system, target_is_directory=True)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            managed = [snapshot, *records, *outside_pages]
            before = {path: path.read_bytes() for path in managed}

            result = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertIn("Vault", report["errors"][0])
            self.assertEqual({path: path.read_bytes() for path in managed}, before)

    def test_apply_rejects_an_index_directory_redirected_into_get_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, original_pages = self.prepare_base_case(root)
            for page in original_pages:
                page.unlink()
            original_pages[0].parent.rmdir()
            system_directory = vault / "系统"
            system_directory.rmdir()
            get_index = vault / "GET NOTES" / "知识库索引"
            get_index.mkdir()
            redirected_pages = [get_index / filename for filename in INDEX_FILENAMES]
            for index, page in enumerate(redirected_pages, start=1):
                page.write_text(f"GET-SOURCE-INDEX-{index}\n", encoding="utf-8")
            system_directory.symlink_to(vault / "GET NOTES", target_is_directory=True)
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            managed = [snapshot, *records, *redirected_pages]
            before = {path: path.read_bytes() for path in managed}

            result = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertIn("GET NOTES", report["errors"][0])
            self.assertEqual({path: path.read_bytes() for path in managed}, before)

    def test_no_change_apply_does_not_rewrite_any_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            managed = [snapshot, *records, *pages]
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }

            result = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "no_changes")
            self.assertEqual(report["changed_file_count"], 0)
            after = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }
            self.assertEqual(after, before)
            self.assertFalse((snapshot.parent / "apply-transaction.json").exists())

            full_validation = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--vault-root",
                    str(vault),
                    *[str(path) for path in records],
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(full_validation.returncode, 0, full_validation.stderr)
            self.assertIn(
                "PASS full GET source-index validation",
                full_validation.stdout,
            )

    def test_ready_preflight_validates_the_full_candidate_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            managed = [snapshot, *records, *pages]
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }
            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )

            report = incremental.preflight_review_draft(args)

            self.assertEqual(report["status"], "ready_to_apply")
            self.assertEqual(report["managed_target_count"], 9)
            self.assertGreater(report["changed_file_count"], 0)
            self.assertEqual(report["source_count"], 2)
            self.assertEqual(
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in managed
                },
                before,
            )
            self.assertFalse((snapshot.parent / "apply-transaction.json").exists())

    def test_no_change_preflight_does_not_validate_or_migrate_old_index_templates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            managed = [snapshot, *records, *pages]
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }
            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )

            report = incremental.preflight_review_draft(args)

            self.assertEqual(report["status"], "no_changes")
            self.assertFalse(report["already_applied"])
            self.assertEqual(report["changed_file_count"], 0)
            self.assertEqual(
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in managed
                },
                before,
            )

    def test_preflight_requires_recover_only_without_touching_a_pending_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            journal = snapshot.parent / "apply-transaction.json"
            journal.write_bytes(b"PENDING-TRANSACTION\n")
            observed = [snapshot, *records, *pages, journal]
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in observed
            }
            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )

            with self.assertRaisesRegex(TransactionError, "recover-only"):
                incremental.preflight_review_draft(args)

            self.assertEqual(
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in observed
                },
                before,
            )

    def test_concurrent_apply_for_the_same_vault_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            managed = [snapshot, *records, *pages]
            before = {path: path.read_bytes() for path in managed}

            with transaction_lock(pages[0].parent):
                result = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertIn("另一个 GET 索引应用", report["errors"][0])
            self.assertEqual({path: path.read_bytes() for path in managed}, before)

    def test_apply_locks_shared_state_even_when_vaults_are_different(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, snapshot, records, _ = self.prepare_base_case(root)
            second_vault = root / "second-vault"
            write_source(
                second_vault,
                "GET NOTES/Notes/2026-09-15/stable.md",
                uid="uid-stable",
                title="stable",
            )
            second_index = second_vault / "系统" / "知识库索引"
            second_index.mkdir(parents=True)
            second_pages = [second_index / filename for filename in INDEX_FILENAMES]
            for index, page in enumerate(second_pages, start=1):
                page.write_text(f"SECOND-INDEX-{index}\n", encoding="utf-8")
            review_draft = self.generate_review_draft(
                root,
                second_vault,
                snapshot,
                records,
            )
            managed = [snapshot, *records, *second_pages]
            before = {path: path.read_bytes() for path in managed}

            with transaction_lock(snapshot.parent):
                result = self.run_apply(
                    second_vault,
                    snapshot,
                    review_draft,
                    records,
                )

            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertIn("另一个 GET 索引应用", report["errors"][0])
            self.assertEqual({path: path.read_bytes() for path in managed}, before)

    def test_interrupted_apply_recovers_after_switching_from_an_ancestor_alias(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = Path(temp_dir)
            root = container / "case"
            root.mkdir()
            vault, snapshot, records, _ = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            alias_root = container / "case-alias"
            alias_root.symlink_to(root, target_is_directory=True)
            alias_snapshot = alias_root / "state" / snapshot.name
            real_replace = replace_files_transactionally

            def crash_after_first_replace(contents, **kwargs):
                return real_replace(
                    contents,
                    **{
                        **kwargs,
                        "after_replace": lambda _path, index: (
                            (_ for _ in ()).throw(SystemExit(93))
                            if index == 1
                            else None
                        ),
                    },
                )

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=alias_snapshot,
                review_draft=review_draft,
                records=records,
            )
            with mock.patch.object(
                incremental,
                "replace_files_transactionally",
                side_effect=crash_after_first_replace,
            ):
                with self.assertRaisesRegex(SystemExit, "93"):
                    incremental.apply_review_draft(args)

            journal = snapshot.parent / "apply-transaction.json"
            self.assertTrue(journal.exists())

            recovered = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(recovered.returncode, 0, recovered.stdout)
            report = json.loads(recovered.stdout)
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["recovered_transaction"], "rolled_back")
            self.assertFalse(journal.exists())

    def test_apply_keeps_canonical_external_inputs_after_ancestor_alias_redirect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside-inputs"
            outside.mkdir()
            vault, snapshot, records, _ = self.prepare_base_case(outside)
            physical_vault = root / "vault"
            vault.rename(physical_vault)
            vault = physical_vault
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(
                outside,
                vault,
                snapshot,
                records,
            )
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            alias_root = root / "outside-inputs-alias"
            alias_root.symlink_to(outside, target_is_directory=True)
            alias_snapshot = alias_root / snapshot.relative_to(outside)
            alias_review_draft = alias_root / review_draft.relative_to(outside)
            alias_records = [
                alias_root / record.relative_to(outside)
                for record in records
            ]

            get_sources = vault / "GET NOTES"
            mirror_snapshot = get_sources / snapshot.relative_to(outside)
            mirror_review_draft = get_sources / review_draft.relative_to(outside)
            mirror_records = [
                get_sources / record.relative_to(outside)
                for record in records
            ]
            mirror_snapshot.parent.mkdir(parents=True)
            for mirror in [mirror_snapshot, mirror_review_draft, *mirror_records]:
                mirror.write_bytes(f"POISON-{mirror.name}\n".encode())

            def get_sources_state() -> dict[Path, tuple[str, bytes | None, int]]:
                state: dict[Path, tuple[str, bytes | None, int]] = {}
                for path in [get_sources, *sorted(get_sources.rglob("*"))]:
                    details = path.lstat()
                    relative_path = path.relative_to(get_sources)
                    if path.is_file():
                        state[relative_path] = (
                            "file",
                            path.read_bytes(),
                            details.st_mtime_ns,
                        )
                    elif path.is_dir():
                        state[relative_path] = ("directory", None, details.st_mtime_ns)
                    else:
                        state[relative_path] = ("other", None, details.st_mtime_ns)
                return state

            get_sources_before = get_sources_state()
            external_before = {
                path: path.read_bytes()
                for path in [snapshot, review_draft, *records]
            }
            real_normalize = incremental.normalize_apply_arguments
            normalized: list[
                tuple[Path, Path, Path, list[Path], list[Path], Path]
            ] = []

            def normalize_then_redirect(arguments):
                result = real_normalize(arguments)
                normalized.append(result)
                alias_root.unlink()
                alias_root.symlink_to(get_sources, target_is_directory=True)
                return result

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=alias_snapshot,
                review_draft=alias_review_draft,
                records=alias_records,
            )
            with mock.patch.object(
                incremental,
                "normalize_apply_arguments",
                side_effect=normalize_then_redirect,
            ):
                report = incremental.apply_review_draft(args)

            self.assertEqual(report["status"], "applied")
            self.assertEqual(len(normalized), 1)
            _, normalized_snapshot, normalized_review, normalized_records, _, _ = (
                normalized[0]
            )
            self.assertEqual(normalized_snapshot, snapshot.resolve(strict=True))
            self.assertEqual(normalized_review, review_draft.resolve(strict=True))
            self.assertEqual(
                normalized_records,
                [record.resolve(strict=True) for record in records],
            )
            self.assertEqual(alias_root.resolve(), get_sources.resolve())
            self.assertNotEqual(snapshot.read_bytes(), external_before[snapshot])
            self.assertNotEqual(records[-1].read_bytes(), external_before[records[-1]])
            self.assertEqual(
                review_draft.read_bytes(),
                external_before[review_draft],
            )
            self.assertEqual(get_sources_state(), get_sources_before)
            self.assertFalse((mirror_snapshot.parent / "apply-transaction.json").exists())

    def test_multidirectory_lock_survives_visible_index_directory_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, _, targets, _ = self.prepare_transaction_case(root)
            index_directory = next(
                path.parent for path in targets if path.name.startswith("GET 来源索引")
            )
            preserved_index = root / "preserved-index"

            with transaction_lock(state, index_directory):
                index_directory.rename(preserved_index)
                index_directory.mkdir()
                try:
                    with self.assertRaisesRegex(
                        TransactionError,
                        "另一个 GET 索引应用",
                    ):
                        with transaction_lock(state, index_directory):
                            pass
                finally:
                    index_directory.rmdir()
                    preserved_index.rename(index_directory)

    def assert_interrupted_transaction_residuals_are_git_ignored(
        self, snapshot_path: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            subprocess.run(
                ["git", "init", "--quiet", str(root)],
                check=True, capture_output=True, timeout=5,
            )
            (root / ".gitignore").write_bytes((PROJECT_ROOT / ".gitignore").read_bytes())
            snapshot = root / snapshot_path
            state = snapshot.parent
            state.mkdir()
            record = root / "part-1.jsonl"
            old_metadata = b"SYNTHETIC-PRIVATE-OLD-METADATA\n"
            record.write_bytes(old_metadata)
            snapshot.write_bytes(b"SYNTHETIC-OLD-SNAPSHOT\n")
            journal = state / "apply-transaction.json"

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    {record: b"SYNTHETIC-NEW-METADATA\n", snapshot: b"SYNTHETIC-NEW-SNAPSHOT\n"},
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=lambda _path, index: (_ for _ in ()).throw(SystemExit())
                    if index == 1 else None,
                )

            backups = list(state.glob("apply-transaction-*/old-*.bin"))
            stages = list(root.rglob(".get-index-apply-*.tmp"))
            self.assertTrue(backups)
            self.assertTrue(stages)
            self.assertIn(old_metadata, [path.read_bytes() for path in backups])
            self.assertTrue(journal.exists())
            # Journal publication and recovery can also stop with a temporary file.
            other_temps = [
                directory / f".get-index-{kind}-synthetic.tmp"
                for directory in (root, state)
                for kind in ("apply", "journal", "recovery")
            ]
            for path in other_temps:
                path.write_bytes(b"SYNTHETIC-TRANSACTION-RESIDUE\n")
            paths = [record, snapshot, journal, *backups, *stages, *other_temps]
            relative_paths = [path.relative_to(root).as_posix() for path in paths]
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--no-index", "--stdin"],
                input="\n".join(relative_paths) + "\n",
                text=True, capture_output=True, check=False, timeout=5,
            )
            self.assertIn(ignored.returncode, (0, 1), ignored.stderr)
            self.assertEqual(set(relative_paths) - set(ignored.stdout.splitlines()), set())
            # Code and test files must remain visible to Git.
            visible = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--no-index", "--stdin"],
                input=("get_index_transaction.py\ntests/test_atomic_apply.py\n"
                       "snapshots/helper.py\napply-transaction-helper.py\n"),
                text=True, capture_output=True, check=False, timeout=5,
            )
            self.assertEqual(visible.returncode, 1, visible.stderr)
            self.assertEqual(visible.stdout, "")

    def test_interrupted_transaction_residuals_are_git_ignored(self) -> None:
        self.assert_interrupted_transaction_residuals_are_git_ignored(
            "state/accepted-snapshot.json"
        )

    def test_nondefault_snapshot_transaction_residuals_are_git_ignored(self) -> None:
        self.assert_interrupted_transaction_residuals_are_git_ignored(
            "snapshots/part-accepted.jsonl"
        )

    def test_interrupted_transaction_before_snapshot_is_exactly_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            record = next(path for path in targets if path.name == "part.jsonl")
            journal = state / "apply-transaction.json"

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=lambda _path, index: (_ for _ in ()).throw(SystemExit())
                    if index == 1
                    else None,
                )

            self.assertTrue(journal.exists())
            self.assertEqual(record.read_bytes(), b"NEW-RECORD\n")
            self.assertEqual(snapshot.read_bytes(), b"OLD-SNAPSHOT\n")
            with transaction_lock(state):
                recovery = recover_transaction(journal, set(targets), snapshot)

            self.assertEqual(recovery, "rolled_back")
            after = {
                path: (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_mtime_ns)
                for path in targets
            }
            self.assertEqual(after, before)
            self.assertFalse(journal.exists())
            self.assertFalse(any(root.rglob(".get-index-*.tmp")))
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_handled_replacement_failure_rolls_back_in_the_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            def fail_after_first_replace(_path: Path, index: int) -> None:
                if index == 1:
                    raise RuntimeError("synthetic replacement failure")

            with self.assertRaisesRegex(TransactionError, "已恢复原状态"):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=fail_after_first_replace,
                )

            after = {
                path: (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_mtime_ns)
                for path in targets
            }
            self.assertEqual(after, before)
            self.assertFalse(journal.exists())
            self.assertFalse(any(root.rglob(".get-index-*.tmp")))
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_interruption_during_transaction_prepare_leaves_no_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"
            real_create = transaction._create_file_at
            call_count = 0

            def interrupt_third_create(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 3:
                    raise SystemExit(88)
                return real_create(*args, **kwargs)

            with mock.patch.object(
                transaction,
                "_create_file_at",
                side_effect=interrupt_third_create,
            ):
                with self.assertRaisesRegex(SystemExit, "88"):
                    replace_files_transactionally(
                        targets,
                        commit_marker=snapshot,
                        journal_path=journal,
                    )

            after = {
                path: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in targets
            }
            self.assertEqual(after, before)
            self.assertFalse(journal.exists())
            self.assertFalse(any(root.rglob(".get-index-*.tmp")))
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_state_directory_fsync_failure_during_prepare_is_controlled_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"
            real_mkdir = transaction.os.mkdir
            real_fsync = transaction.os.fsync
            backup_created = False
            failed = False

            def observe_backup_mkdir(path, *args, **kwargs):
                nonlocal backup_created
                result = real_mkdir(path, *args, **kwargs)
                if isinstance(path, str) and path.startswith("apply-transaction-"):
                    backup_created = True
                return result

            def fail_first_state_fsync(descriptor):
                nonlocal failed
                if backup_created and not failed:
                    failed = True
                    raise OSError("synthetic state fsync failure")
                return real_fsync(descriptor)

            with mock.patch.object(
                transaction.os,
                "mkdir",
                side_effect=observe_backup_mkdir,
            ), mock.patch.object(
                transaction.os,
                "fsync",
                side_effect=fail_first_state_fsync,
            ):
                with self.assertRaisesRegex(TransactionError, "备份目录"):
                    replace_files_transactionally(
                        targets,
                        commit_marker=snapshot,
                        journal_path=journal,
                    )

            after = {
                path: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in targets
            }
            self.assertEqual(after, before)
            self.assertFalse(journal.exists())
            self.assertFalse(any(root.rglob(".get-index-*.tmp")))
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_backup_directory_open_failure_during_prepare_is_controlled_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"
            real_open = transaction.os.open

            def fail_backup_open(path, *args, **kwargs):
                if isinstance(path, str) and path.startswith("apply-transaction-"):
                    raise OSError("synthetic backup open failure")
                return real_open(path, *args, **kwargs)

            with mock.patch.object(
                transaction.os,
                "open",
                side_effect=fail_backup_open,
            ):
                with self.assertRaisesRegex(TransactionError, "备份目录"):
                    replace_files_transactionally(
                        targets,
                        commit_marker=snapshot,
                        journal_path=journal,
                    )

            after = {
                path: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in targets
            }
            self.assertEqual(after, before)
            self.assertFalse(journal.exists())
            self.assertFalse(any(root.rglob(".get-index-*.tmp")))
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_journal_replace_failure_during_prepare_is_controlled_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"
            real_replace = transaction.os.replace

            def fail_journal_replace(source, destination, *args, **kwargs):
                if destination == journal.name:
                    raise OSError("synthetic journal replace failure")
                return real_replace(source, destination, *args, **kwargs)

            with mock.patch.object(
                transaction.os,
                "replace",
                side_effect=fail_journal_replace,
            ):
                with self.assertRaisesRegex(TransactionError, "事务日志"):
                    replace_files_transactionally(
                        targets,
                        commit_marker=snapshot,
                        journal_path=journal,
                    )

            after = {
                path: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in targets
            }
            self.assertEqual(after, before)
            self.assertFalse(journal.exists())
            self.assertFalse(any(root.rglob(".get-index-*.tmp")))
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_journal_fsync_failure_preserves_a_recoverable_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"
            real_replace = transaction.os.replace
            real_fsync = transaction.os.fsync
            journal_published = False
            failed = False

            def observe_journal_replace(source, destination, *args, **kwargs):
                nonlocal journal_published
                result = real_replace(source, destination, *args, **kwargs)
                if destination == journal.name:
                    journal_published = True
                return result

            def fail_journal_directory_fsync(descriptor):
                nonlocal failed
                if journal_published and not failed:
                    failed = True
                    raise OSError("synthetic journal fsync failure")
                return real_fsync(descriptor)

            with mock.patch.object(
                transaction.os,
                "replace",
                side_effect=observe_journal_replace,
            ), mock.patch.object(
                transaction.os,
                "fsync",
                side_effect=fail_journal_directory_fsync,
            ):
                with self.assertRaisesRegex(TransactionError, "已保留"):
                    replace_files_transactionally(
                        targets,
                        commit_marker=snapshot,
                        journal_path=journal,
                    )

            self.assertTrue(journal.exists())
            self.assertTrue(any(state.glob("apply-transaction-*")))
            self.assertEqual(
                {
                    path: (
                        path.read_bytes(),
                        path.stat().st_mode & 0o777,
                        path.stat().st_mtime_ns,
                    )
                    for path in targets
                },
                before,
            )

            recovery = recover_transaction(journal, set(targets), snapshot)

            self.assertEqual(recovery, "rolled_back")
            self.assertFalse(journal.exists())
            self.assertFalse(any(root.rglob(".get-index-*.tmp")))
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_interruption_immediately_after_journal_replace_preserves_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"
            real_replace = transaction.os.replace

            def interrupt_after_journal_replace(source, destination, *args, **kwargs):
                result = real_replace(source, destination, *args, **kwargs)
                if destination == journal.name:
                    raise SystemExit(91)
                return result

            with mock.patch.object(
                transaction.os,
                "replace",
                side_effect=interrupt_after_journal_replace,
            ):
                with self.assertRaisesRegex(TransactionError, "已保留"):
                    replace_files_transactionally(
                        targets,
                        commit_marker=snapshot,
                        journal_path=journal,
                    )

            self.assertTrue(journal.exists())
            self.assertEqual(
                {
                    path: (
                        path.read_bytes(),
                        path.stat().st_mode & 0o777,
                        path.stat().st_mtime_ns,
                    )
                    for path in targets
                },
                before,
            )
            self.assertEqual(
                recover_transaction(journal, set(targets), snapshot),
                "rolled_back",
            )
            self.assertFalse(journal.exists())

    def test_transaction_rejects_symlinked_and_hardlinked_targets_without_writing(self) -> None:
        for target_kind in ("symlink", "hardlink"):
            with self.subTest(target_kind=target_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                state, snapshot, targets, _ = self.prepare_transaction_case(root)
                target = next(path for path in targets if path.name == "part.jsonl")
                original = target.read_bytes()
                other_targets_before = {
                    path: path.read_bytes() for path in targets if path != target
                }
                alias = target.with_name("alias.jsonl")
                if target_kind == "symlink":
                    target.rename(alias)
                    target.symlink_to(alias)
                else:
                    os.link(target, alias)

                with self.assertRaisesRegex(TransactionError, "单硬链接普通文件"):
                    replace_files_transactionally(
                        targets,
                        commit_marker=snapshot,
                        journal_path=state / "apply-transaction.json",
                    )

                self.assertEqual(alias.read_bytes(), original)
                self.assertEqual(
                    {path: path.read_bytes() for path in targets if path != target},
                    other_targets_before,
                )
                self.assertFalse((state / "apply-transaction.json").exists())

    def test_interrupted_transaction_after_snapshot_is_kept_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            def stop_after_snapshot(path: Path, _index: int) -> None:
                if path == snapshot:
                    raise SystemExit()

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=stop_after_snapshot,
                )

            self.assertTrue(journal.exists())
            self.assertTrue(all(path.read_bytes() == content for path, content in targets.items()))
            with transaction_lock(state):
                recovery = recover_transaction(journal, set(targets), snapshot)

            self.assertEqual(recovery, "committed")
            self.assertTrue(all(path.read_bytes() == content for path, content in targets.items()))
            self.assertFalse(journal.exists())
            self.assertFalse(any(root.rglob(".get-index-*.tmp")))
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_recovery_rejects_a_journal_that_names_an_unrelated_cleanup_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            def stop_after_snapshot(path: Path, _index: int) -> None:
                if path == snapshot:
                    raise SystemExit()

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=stop_after_snapshot,
                )

            payload = json.loads(journal.read_text(encoding="utf-8"))
            target_parent = Path(payload["entries"][0]["target"]).parent
            unrelated = target_parent / "unrelated-important.md"
            unrelated.write_text("KEEP\n", encoding="utf-8")
            payload["entries"][0]["stage_name"] = unrelated.name
            journal.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(TransactionError, "临时名称无效"):
                recover_transaction(journal, set(targets), snapshot)

            self.assertEqual(unrelated.read_text(encoding="utf-8"), "KEEP\n")
            self.assertTrue(journal.exists())

    def test_recovery_does_not_follow_a_swapped_backup_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            def stop_after_snapshot(path: Path, _index: int) -> None:
                if path == snapshot:
                    raise SystemExit()

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=stop_after_snapshot,
                )

            payload = json.loads(journal.read_text(encoding="utf-8"))
            backup_directory = state / payload["backup_dir"]
            preserved_backup = state / "preserved-backup"
            backup_directory.rename(preserved_backup)
            outside = root / "outside"
            outside.mkdir()
            outside_files = []
            for entry in payload["entries"]:
                outside_file = outside / entry["backup_name"]
                outside_file.write_text("KEEP\n", encoding="utf-8")
                outside_files.append(outside_file)
            backup_directory.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(TransactionError, "备份目录"):
                recover_transaction(journal, set(targets), snapshot)

            self.assertTrue(all(path.read_text(encoding="utf-8") == "KEEP\n" for path in outside_files))
            self.assertTrue(journal.exists())

    def test_recovery_cleanup_is_idempotent_after_every_target_is_already_old(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=lambda _path, index: (_ for _ in ()).throw(SystemExit())
                    if index == 1
                    else None,
                )

            payload = json.loads(journal.read_text(encoding="utf-8"))
            for path, (content, mode, mtime) in before.items():
                path.write_bytes(content)
                os.chmod(path, mode)
                os.utime(path, ns=(mtime, mtime))
            missing_backup = state / payload["backup_dir"] / payload["entries"][0]["backup_name"]
            missing_backup.unlink()

            recovery = recover_transaction(journal, set(targets), snapshot)

            self.assertEqual(recovery, "rolled_back")
            self.assertFalse(journal.exists())
            self.assertFalse(any(state.glob("apply-transaction-*")))

    def test_recovery_rejects_marker_new_with_an_old_non_marker_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, before = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=lambda path, _index: (_ for _ in ()).throw(SystemExit())
                    if path == snapshot
                    else None,
                )
            non_marker = next(path for path in targets if path != snapshot)
            non_marker.write_bytes(before[non_marker][0])

            with self.assertRaisesRegex(TransactionError, "混合状态"):
                recover_transaction(journal, set(targets), snapshot)

            self.assertTrue(journal.exists())

    def test_recovery_rejects_unknown_target_content_and_keeps_the_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=lambda _path, index: (_ for _ in ()).throw(SystemExit())
                    if index == 1
                    else None,
                )
            changed_target = next(path for path in targets if path != snapshot)
            changed_target.write_bytes(b"UNKNOWN\n")

            with self.assertRaisesRegex(TransactionError, "未知内容"):
                recover_transaction(journal, set(targets), snapshot)

            self.assertTrue(journal.exists())

    def test_recovery_does_not_overwrite_content_changed_after_its_initial_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=lambda _path, index: (_ for _ in ()).throw(SystemExit())
                    if index == 1
                    else None,
                )
            changed_target = next(
                path
                for path, expected in targets.items()
                if path != snapshot and path.read_bytes() == expected
            )
            real_restore = transaction._restore_entry

            def change_target_before_restore(entry, backup_content, *args, **kwargs):
                Path(entry["target"]).write_bytes(b"EXTERNAL-CONCURRENT-CONTENT\n")
                return real_restore(entry, backup_content, *args, **kwargs)

            with mock.patch.object(
                transaction,
                "_restore_entry",
                side_effect=change_target_before_restore,
            ):
                with self.assertRaisesRegex(TransactionError, "恢复期间发生变化"):
                    recover_transaction(journal, set(targets), snapshot)

            self.assertEqual(
                changed_target.read_bytes(),
                b"EXTERNAL-CONCURRENT-CONTENT\n",
            )
            self.assertTrue(journal.exists())

    def test_malformed_journal_types_fail_as_a_controlled_transaction_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            with self.assertRaises(SystemExit):
                replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                    after_replace=lambda _path, index: (_ for _ in ()).throw(SystemExit())
                    if index == 1
                    else None,
                )
            payload = json.loads(journal.read_text(encoding="utf-8"))
            payload["entries"][0]["target"] = []
            journal.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(TransactionError, "目标"):
                recover_transaction(journal, set(targets), snapshot)

            self.assertTrue(journal.exists())

    def test_error_after_snapshot_commit_is_reported_as_committed_not_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"

            def fail_after_snapshot(path: Path, _index: int) -> None:
                if path == snapshot:
                    raise RuntimeError("post-commit synthetic failure")

            changed = replace_files_transactionally(
                targets,
                commit_marker=snapshot,
                journal_path=journal,
                after_replace=fail_after_snapshot,
            )

            self.assertEqual(set(changed), set(targets))
            self.assertTrue(
                all(path.read_bytes() == content for path, content in targets.items())
            )
            self.assertFalse(journal.exists())

    def test_snapshot_directory_fsync_failure_after_replace_is_not_reported_as_rollback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"
            real_replace = transaction.os.replace
            real_fsync = transaction.os.fsync
            snapshot_replaced = False
            failed = False

            def observe_snapshot_replace(source, destination, *args, **kwargs):
                nonlocal snapshot_replaced
                result = real_replace(source, destination, *args, **kwargs)
                if destination == snapshot.name:
                    snapshot_replaced = True
                return result

            def fail_first_fsync_after_snapshot_replace(descriptor):
                nonlocal failed
                if snapshot_replaced and not failed:
                    failed = True
                    raise OSError("synthetic snapshot directory fsync failure")
                return real_fsync(descriptor)

            with mock.patch.object(
                transaction.os,
                "replace",
                side_effect=observe_snapshot_replace,
            ), mock.patch.object(
                transaction.os,
                "fsync",
                side_effect=fail_first_fsync_after_snapshot_replace,
            ):
                changed = replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                )

            self.assertEqual(set(changed), set(targets))
            self.assertTrue(
                all(path.read_bytes() == content for path, content in targets.items())
            )
            self.assertFalse(journal.exists())

    def test_ambiguous_snapshot_replace_error_uses_recovered_committed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state, snapshot, targets, _ = self.prepare_transaction_case(root)
            journal = state / "apply-transaction.json"
            real_replace = transaction.os.replace

            def fail_after_snapshot_replace(source, destination, *args, **kwargs):
                result = real_replace(source, destination, *args, **kwargs)
                if destination == snapshot.name:
                    raise OSError("synthetic ambiguous snapshot replace failure")
                return result

            with mock.patch.object(
                transaction.os,
                "replace",
                side_effect=fail_after_snapshot_replace,
            ):
                changed = replace_files_transactionally(
                    targets,
                    commit_marker=snapshot,
                    journal_path=journal,
                )

            self.assertEqual(set(changed), set(targets))
            self.assertTrue(
                all(path.read_bytes() == content for path, content in targets.items())
            )
            self.assertFalse(journal.exists())

    def test_cli_reports_success_after_recovering_an_already_committed_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, _ = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            real_replace = replace_files_transactionally

            def crash_after_commit_marker(
                contents: dict[Path, bytes],
                *,
                commit_marker: Path,
                journal_path: Path,
                before_commit,
                expected_old_sha256,
                expected_directory_identities,
            ) -> list[Path]:
                def stop_after_snapshot(path: Path, _index: int) -> None:
                    if path == commit_marker:
                        raise SystemExit(77)

                return real_replace(
                    contents,
                    commit_marker=commit_marker,
                    journal_path=journal_path,
                    before_commit=before_commit,
                    after_replace=stop_after_snapshot,
                    expected_old_sha256=expected_old_sha256,
                    expected_directory_identities=expected_directory_identities,
                )

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )
            with mock.patch.object(
                incremental,
                "replace_files_transactionally",
                side_effect=crash_after_commit_marker,
            ):
                with self.assertRaisesRegex(SystemExit, "77"):
                    incremental.apply_review_draft(args)

            journal = snapshot.parent / "apply-transaction.json"
            self.assertTrue(journal.exists())

            result = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "applied_recovered")
            self.assertEqual(report["recovered_transaction"], "committed")
            self.assertEqual(report["changed_file_count"], 0)
            self.assertEqual(report["source_count"], 2)
            self.assertFalse(journal.exists())

    def test_recover_only_rolls_back_without_reapplying_or_a_review_draft(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            managed = [snapshot, *records, *pages]
            before = {
                path: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in managed
            }
            real_replace = replace_files_transactionally

            def crash_before_commit_marker(contents, **kwargs):
                def stop_after_first_target(path: Path, _index: int) -> None:
                    if path != kwargs["commit_marker"]:
                        raise SystemExit(71)

                return real_replace(
                    contents,
                    **{**kwargs, "after_replace": stop_after_first_target},
                )

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )
            with mock.patch.object(
                incremental,
                "replace_files_transactionally",
                side_effect=crash_before_commit_marker,
            ):
                with self.assertRaisesRegex(SystemExit, "71"):
                    incremental.apply_review_draft(args)

            journal = snapshot.parent / "apply-transaction.json"
            self.assertTrue(journal.exists())
            review_draft.unlink()

            report = incremental.recover_apply_transaction(args)

            self.assertEqual(report["status"], "rolled_back")
            self.assertEqual(report["recovered_transaction"], "rolled_back")
            self.assertEqual(report["pending_change_count"], 1)
            self.assertEqual(
                {
                    path: (
                        path.read_bytes(),
                        path.stat().st_mode & 0o777,
                        path.stat().st_mtime_ns,
                    )
                    for path in managed
                },
                before,
            )
            self.assertFalse(journal.exists())
            next_scan = self.run_cli(
                "scan",
                "--vault-root",
                str(vault),
                "--snapshot",
                str(snapshot),
                "--format",
                "json",
            )
            self.assertEqual(next_scan.returncode, 0, next_scan.stderr)
            self.assertEqual(json.loads(next_scan.stdout)["counts"]["added"], 1)

    def test_recover_only_verifies_and_cleans_a_post_marker_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            real_replace = replace_files_transactionally

            def crash_after_commit_marker(contents, **kwargs):
                def stop_after_snapshot(path: Path, _index: int) -> None:
                    if path == kwargs["commit_marker"]:
                        raise SystemExit(73)

                return real_replace(
                    contents,
                    **{**kwargs, "after_replace": stop_after_snapshot},
                )

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )
            with mock.patch.object(
                incremental,
                "replace_files_transactionally",
                side_effect=crash_after_commit_marker,
            ):
                with self.assertRaisesRegex(SystemExit, "73"):
                    incremental.apply_review_draft(args)

            journal = snapshot.parent / "apply-transaction.json"
            self.assertTrue(journal.exists())

            report = incremental.recover_apply_transaction(args)

            self.assertEqual(report["status"], "applied_recovered")
            self.assertEqual(report["recovered_transaction"], "committed")
            self.assertEqual(report["pending_change_count"], 0)
            self.assertEqual(report["source_count"], 2)
            self.assertFalse(journal.exists())
            self.assertTrue(all(b"EXISTING-INDEX" not in page.read_bytes() for page in pages))
            next_scan = self.run_cli(
                "scan",
                "--vault-root",
                str(vault),
                "--snapshot",
                str(snapshot),
                "--format",
                "json",
            )
            self.assertEqual(next_scan.returncode, 0, next_scan.stderr)
            self.assertEqual(json.loads(next_scan.stdout)["status"], "unchanged")

    def test_recover_only_keeps_a_committed_journal_until_semantic_validation_passes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            real_replace = replace_files_transactionally

            def crash_after_commit_marker(contents, **kwargs):
                def stop_after_snapshot(path: Path, _index: int) -> None:
                    if path == kwargs["commit_marker"]:
                        raise SystemExit(79)

                return real_replace(
                    contents,
                    **{**kwargs, "after_replace": stop_after_snapshot},
                )

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )
            with mock.patch.object(
                incremental,
                "replace_files_transactionally",
                side_effect=crash_after_commit_marker,
            ):
                with self.assertRaisesRegex(SystemExit, "79"):
                    incremental.apply_review_draft(args)

            journal = snapshot.parent / "apply-transaction.json"
            tampered = b"SEMANTICALLY-INVALID-INDEX\n"
            journal_payload = json.loads(journal.read_text(encoding="utf-8"))
            page_entry = next(
                entry
                for entry in journal_payload["entries"]
                if Path(entry["target"]).parent.name == "知识库索引"
            )
            Path(page_entry["target"]).write_bytes(tampered)
            page_entry["new_sha256"] = hashlib.sha256(tampered).hexdigest()
            journal.write_text(
                json.dumps(journal_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(incremental.ScanError, "覆盖索引"):
                incremental.recover_apply_transaction(args)

            self.assertTrue(journal.exists())

    def test_apply_refuses_if_a_validated_record_changes_before_transaction_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            snapshot_before = snapshot.read_bytes()
            pages_before = {path: path.read_bytes() for path in pages}
            real_replace = replace_files_transactionally
            externally_changed = records[0].read_bytes() + b"\n"

            def change_record_then_prepare(contents, **kwargs):
                records[0].write_bytes(externally_changed)
                return real_replace(contents, **kwargs)

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )
            with mock.patch.object(
                incremental,
                "replace_files_transactionally",
                side_effect=change_record_then_prepare,
            ):
                with self.assertRaisesRegex(TransactionError, "校验后发生变化"):
                    incremental.apply_review_draft(args)

            self.assertEqual(records[0].read_bytes(), externally_changed)
            self.assertEqual(snapshot.read_bytes(), snapshot_before)
            self.assertEqual({path: path.read_bytes() for path in pages}, pages_before)
            self.assertFalse((snapshot.parent / "apply-transaction.json").exists())

    def test_get_change_before_snapshot_commit_rolls_back_every_managed_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            new_source = write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            managed = [snapshot, *records, *pages]
            before = {
                path: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in managed
            }
            real_replace = replace_files_transactionally

            def change_get_before_commit(contents, **kwargs):
                original_before_commit = kwargs["before_commit"]

                def changed_source_check() -> None:
                    new_source.write_text(
                        new_source.read_text(encoding="utf-8") + "late change\n",
                        encoding="utf-8",
                    )
                    original_before_commit()

                return real_replace(
                    contents,
                    **{**kwargs, "before_commit": changed_source_check},
                )

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )
            with mock.patch.object(
                incremental,
                "replace_files_transactionally",
                side_effect=change_get_before_commit,
            ):
                with self.assertRaisesRegex(TransactionError, "GET 来源"):
                    incremental.apply_review_draft(args)

            after = {
                path: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in managed
            }
            self.assertEqual(after, before)
            self.assertFalse((snapshot.parent / "apply-transaction.json").exists())

    def test_get_change_after_commit_is_reported_as_a_new_pending_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, _ = self.prepare_base_case(root)
            new_source = write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            real_replace = replace_files_transactionally

            def change_get_after_commit(contents, **kwargs):
                changed = real_replace(contents, **kwargs)
                new_source.write_text(
                    new_source.read_text(encoding="utf-8") + "post-commit delta\n",
                    encoding="utf-8",
                )
                return changed

            args = argparse.Namespace(
                vault_root=vault,
                snapshot=snapshot,
                review_draft=review_draft,
                records=records,
            )
            with mock.patch.object(
                incremental,
                "replace_files_transactionally",
                side_effect=change_get_after_commit,
            ):
                report = incremental.apply_review_draft(args)

            self.assertEqual(report["status"], "applied_with_new_changes")
            self.assertEqual(report["pending_change_count"], 1)
            self.assertTrue(report["snapshot_advanced"])
            next_scan = self.run_cli(
                "scan",
                "--vault-root",
                str(vault),
                "--snapshot",
                str(snapshot),
                "--format",
                "json",
            )
            self.assertEqual(next_scan.returncode, 0, next_scan.stderr)
            self.assertEqual(
                json.loads(next_scan.stdout)["counts"]["content_only_changed"],
                1,
            )

    def test_invalid_review_inputs_fail_before_writing_any_managed_target(self) -> None:
        mutation_names = (
            "incomplete_review",
            "tampered_source_fact",
            "stale_fingerprint",
            "duplicate_draft",
        )
        for mutation_name in mutation_names:
            with self.subTest(mutation=mutation_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault, snapshot, records, pages = self.prepare_base_case(root)
                source = write_source(
                    vault,
                    "GET NOTES/Notes/2026-10-01/new.md",
                    uid="uid-new",
                    title="new",
                    created="2026-10-01",
                )
                review_draft = self.generate_review_draft(root, vault, snapshot, records)
                payload = json.loads(review_draft.read_text(encoding="utf-8"))
                if mutation_name != "incomplete_review":
                    payload["drafts"][0].update(
                        {
                            "domain": "工作/个人",
                            "tags": ["项目与流程"],
                            "review_state": "规则初分",
                            "flag": "none",
                            "unresolved_fields": [],
                        }
                    )
                if mutation_name == "tampered_source_fact":
                    payload["drafts"][0]["title"] = "tampered"
                elif mutation_name == "stale_fingerprint":
                    payload["current_fingerprint"] = "sha256:" + "0" * 64
                elif mutation_name == "duplicate_draft":
                    payload["drafts"].append(dict(payload["drafts"][0]))
                review_draft.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                managed = [snapshot, *records, *pages, source]
                before = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in managed
                }

                result = self.run_apply(vault, snapshot, review_draft, records)

                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(json.loads(result.stdout)["status"], "error")
                after = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in managed
                }
                self.assertEqual(after, before)
                self.assertFalse((snapshot.parent / "apply-transaction.json").exists())

    def test_reviewed_added_source_is_applied_with_dynamic_private_safe_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            new_source = write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title=(
                    "PRIVATE-RESTRICTED-TITLE "
                    "HTTPS://example.com/note?token=PRIVATE-QUERY"
                ),
                created="2026-10-01",
                tags=("raw-private-tag",),
                body="PRIVATE-SOURCE-BODY",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            draft = payload["drafts"][0]
            draft["domain"] = "生活"
            draft["tags"] = ["成长与生活"]
            draft["review_state"] = "规则初分"
            draft["flag"] = "none"
            draft["unresolved_fields"] = []
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            source_before = (new_source.read_bytes(), new_source.stat().st_mtime_ns)

            result = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["source_count"], 2)
            self.assertGreater(report["changed_file_count"], 0)
            self.assertEqual(records[0].read_text(encoding="utf-8").count("\n"), 1)
            self.assertEqual(records[1].read_bytes(), b"")
            added_rows = [
                json.loads(line)
                for line in records[2].read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(added_rows), 1)
            self.assertEqual(added_rows[0]["relative_path"], "GET NOTES/Notes/2026-10-01/new.md")
            self.assertEqual(added_rows[0]["domain"], "生活")
            self.assertEqual(added_rows[0]["tags"], ["成长与生活"])
            self.assertEqual(added_rows[0]["review_state"], "规则初分")
            self.assertEqual(
                added_rows[0]["title"],
                "PRIVATE-RESTRICTED-TITLE https://example.com/note",
            )

            validated = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--vault-root",
                    str(vault),
                    *[str(path) for path in records],
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertIn("PASS full GET source-index validation", validated.stdout)
            self.assertNotIn("PRIVATE-QUERY", validated.stdout)
            self.assertNotIn("PRIVATE-QUERY", validated.stderr)

            scan_after = self.run_cli(
                "scan",
                "--vault-root",
                str(vault),
                "--snapshot",
                str(snapshot),
                "--format",
                "json",
            )
            self.assertEqual(scan_after.returncode, 0, scan_after.stderr)
            accepted = json.loads(scan_after.stdout)
            self.assertEqual(accepted["status"], "unchanged")
            self.assertEqual(accepted["current_source_count"], 2)

            rendered = {page.name: page.read_text(encoding="utf-8") for page in pages}
            combined = "\n".join(rendered.values())
            self.assertIn("GET 已完成全量来源覆盖：2/2 篇", rendered[INDEX_FILENAMES[0]])
            self.assertIn("日期范围：2026-09-15 至 2026-10-01", rendered[INDEX_FILENAMES[0]])
            self.assertIn("snapshot_date: 2026-10-01", combined)
            self.assertIn("GET NOTES/Notes/2026-10-01/new", rendered[INDEX_FILENAMES[1]])
            self.assertNotIn("PRIVATE-RESTRICTED-TITLE", combined)
            self.assertNotIn("PRIVATE-QUERY", combined)
            self.assertNotIn("PRIVATE-SOURCE-BODY", combined)
            self.assertNotIn("raw-private-tag", combined)
            self.assertNotIn("168/168", combined)
            self.assertEqual(
                (new_source.read_bytes(), new_source.stat().st_mtime_ns),
                source_before,
            )
            self.assertFalse((snapshot.parent / "apply-transaction.json").exists())

            managed = [snapshot, *records, *pages]
            before_repeat = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }
            repeated = self.run_apply(vault, snapshot, review_draft, records)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            repeated_report = json.loads(repeated.stdout)
            self.assertEqual(repeated_report["status"], "no_changes")
            self.assertTrue(repeated_report["already_applied"])
            self.assertEqual(repeated_report["changed_file_count"], 0)
            after_repeat = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }
            self.assertEqual(after_repeat, before_repeat)

    def test_repeated_apply_fails_closed_when_an_index_page_has_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "生活",
                    "tags": ["成长与生活"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            applied = self.run_apply(vault, snapshot, review_draft, records)
            self.assertEqual(applied.returncode, 0, applied.stderr)

            pages[1].write_text(
                "CORRUPTED PRIVATE-RESTRICTED-TITLE\n",
                encoding="utf-8",
            )
            managed = [snapshot, *records, *pages]
            before_repeat = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }

            repeated = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(repeated.returncode, 2)
            report = json.loads(repeated.stdout)
            self.assertEqual(report["status"], "error")
            self.assertIn("覆盖索引", report["errors"][0])
            after_repeat = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }
            self.assertEqual(after_repeat, before_repeat)

    def test_empty_old_bundle_cannot_claim_that_a_change_was_already_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages = self.prepare_base_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-10-01/new.md",
                uid="uid-new",
                title="new",
                created="2026-10-01",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            payload["drafts"][0].update(
                {
                    "domain": "工作/个人",
                    "tags": ["项目与流程"],
                    "review_state": "规则初分",
                    "flag": "none",
                    "unresolved_fields": [],
                }
            )
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            applied = self.run_apply(vault, snapshot, review_draft, records)
            self.assertEqual(applied.returncode, 0, applied.stderr)

            forged = json.loads(review_draft.read_text(encoding="utf-8"))
            forged["status"] = "no_review_needed"
            forged["drafts"] = []
            forged["notices"] = []
            review_draft.write_text(
                json.dumps(forged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            managed = [snapshot, *records, *pages]
            before_repeat = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }

            repeated = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(repeated.returncode, 2)
            report = json.loads(repeated.stdout)
            self.assertEqual(report["status"], "error")
            self.assertNotIn("no_changes", repeated.stdout)
            after_repeat = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in managed
            }
            self.assertEqual(after_repeat, before_repeat)

    def test_metadata_and_content_changes_apply_together_without_moving_record_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            sources = {
                name: write_source(
                    vault,
                    f"GET NOTES/Notes/2026-09-15/{name}.md",
                    uid=f"uid-{name}",
                    title=name,
                )
                for name in ("stable", "metadata", "content")
            }
            snapshot = root / "state" / "accepted-snapshot.json"
            snapshot.parent.mkdir(parents=True)
            write_snapshot(
                snapshot,
                [
                    snapshot_entry(
                        sources[name],
                        vault,
                        uid=f"uid-{name}",
                        title=name,
                    )
                    for name in ("stable", "metadata", "content")
                ],
            )
            records = [root / f"part-{index}.jsonl" for index in range(1, 4)]
            for path, name in zip(records, ("stable", "metadata", "content"), strict=True):
                write_records(
                    path,
                    [
                        canonical_record(
                            sources[name],
                            vault,
                            uid=f"uid-{name}",
                            title=name,
                        )
                    ],
                )
            index_dir = vault / "系统" / "知识库索引"
            index_dir.mkdir(parents=True)
            pages = [index_dir / filename for filename in INDEX_FILENAMES]
            for index, page in enumerate(pages, start=1):
                page.write_text(f"EXISTING-INDEX-{index}\n", encoding="utf-8")

            sources["metadata"].write_text(
                source_text(uid="uid-metadata", title="metadata updated"),
                encoding="utf-8",
            )
            sources["content"].write_text(
                source_text(
                    uid="uid-content",
                    title="content",
                    body="updated body",
                ),
                encoding="utf-8",
            )
            review_draft = self.generate_review_draft(root, vault, snapshot, records)
            payload = json.loads(review_draft.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["drafts"]), 1)
            self.assertEqual(len(payload["notices"]), 1)
            payload["drafts"][0]["review_state"] = "规则初分"
            review_draft.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            result = self.run_apply(vault, snapshot, review_draft, records)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "applied")
            part_paths = [
                [json.loads(line)["relative_path"] for line in path.read_text(encoding="utf-8").splitlines()]
                for path in records
            ]
            self.assertEqual(
                part_paths,
                [
                    ["GET NOTES/Notes/2026-09-15/stable.md"],
                    ["GET NOTES/Notes/2026-09-15/metadata.md"],
                    ["GET NOTES/Notes/2026-09-15/content.md"],
                ],
            )
            metadata_record = json.loads(records[1].read_text(encoding="utf-8"))
            self.assertEqual(metadata_record["title"], "metadata updated")
            accepted_after = self.run_cli(
                "scan",
                "--vault-root",
                str(vault),
                "--snapshot",
                str(snapshot),
                "--format",
                "json",
            )
            self.assertEqual(accepted_after.returncode, 0, accepted_after.stderr)
            self.assertEqual(json.loads(accepted_after.stdout)["status"], "unchanged")

            before_repeat = {path: path.read_bytes() for path in [snapshot, *records, *pages]}
            repeated = self.run_apply(vault, snapshot, review_draft, records)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["status"], "no_changes")
            self.assertEqual(
                {path: path.read_bytes() for path in [snapshot, *records, *pages]},
                before_repeat,
            )


if __name__ == "__main__":
    unittest.main()
