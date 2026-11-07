from __future__ import annotations

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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "get_index_incremental.py"


def source_text(
    *,
    uid: str,
    title: str,
    created: str = "2026-09-15",
    note_type: str = "recorder_audio",
    tags: tuple[str, ...] = ("topic-a",),
    body: str = "body",
) -> str:
    rendered_tags = ", ".join(tags)
    return "\n".join(
        [
            "---",
            f'uid: "{uid}"',
            f'title: "{title}"',
            f'created: "{created}T08:00:00+08:00"',
            f'note_type: "{note_type}"',
            f"tags: [{rendered_tags}]",
            "---",
            "",
            body,
            "",
        ]
    )


def write_source(vault: Path, relative_path: str, **values: str) -> Path:
    path = vault / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source_text(**values), encoding="utf-8")
    return path


def source_tags_sha256(tags: tuple[str, ...]) -> str:
    canonical = json.dumps(sorted(set(tags)), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_entry(
    path: Path,
    vault: Path,
    *,
    uid: str,
    title: str,
    created: str = "2026-09-15",
    tags: tuple[str, ...] = ("topic-a",),
) -> dict:
    return {
        "relative_path": str(path.relative_to(vault)),
        "uid": uid,
        "title": title,
        "created_date": created,
        "note_type": "recorder_audio",
        "source_tags_sha256": source_tags_sha256(tags),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def write_snapshot(path: Path, entries: list[dict]) -> None:
    payload = {"schema_version": 1, "sources": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class IncrementalScanCliTests(unittest.TestCase):
    def run_scan(
        self,
        vault: Path,
        snapshot: Path,
        output_format: str = "json",
    ) -> subprocess.CompletedProcess[str]:
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
                output_format,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

    def test_no_change_reports_unchanged_and_does_not_write_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in vault.rglob("*") if path.is_file()}
            snapshot_before = (snapshot.read_bytes(), snapshot.stat().st_mtime_ns)

            first_result = self.run_scan(vault, snapshot)
            second_result = self.run_scan(vault, snapshot)

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertEqual(second_result.stdout, first_result.stdout)
            report = json.loads(first_result.stdout)
            self.assertEqual(report["status"], "unchanged")
            self.assertEqual(
                report["counts"],
                {
                    "added": 0,
                    "index_metadata_changed": 0,
                    "content_only_changed": 0,
                    "missing": 0,
                    "unchanged": 1,
                },
            )
            self.assertEqual(report["changes"], [])
            after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in vault.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            self.assertEqual((snapshot.read_bytes(), snapshot.stat().st_mtime_ns), snapshot_before)

    def test_source_paths_reject_obsidian_link_syntax_and_unsafe_characters(
        self,
    ) -> None:
        unsafe_segments = {
            "closing_brackets": "unsafe]]name",
            "alias_separator": "unsafe|name",
            "heading_fragment": "unsafe#name",
            "block_fragment": "unsafe^name",
            "backslash": r"unsafe\name",
            "tab_control": "unsafe\tname",
            "c1_control": "unsafe\x85name",
        }
        for kind, segment in unsafe_segments.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                stable_relative = "GET NOTES/Notes/2026-09-15/stable.md"
                stable = write_source(
                    vault,
                    stable_relative,
                    uid="uid-stable",
                    title="stable",
                )
                snapshot = root / "accepted-snapshot.json"
                write_snapshot(
                    snapshot,
                    [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
                )
                write_source(
                    vault,
                    f"GET NOTES/Notes/2026-09-15/{segment}.md",
                    uid=f"uid-{kind}",
                    title="unsafe",
                )

                result = self.run_scan(vault, snapshot)

                self.assertNotEqual(result.returncode, 0)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "error")

    def test_text_report_uses_domain_terms_and_states_snapshot_is_not_advanced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/added.md",
                uid="uid-added",
                title="added",
            )

            result = self.run_scan(vault, snapshot, "text")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GET 来源", result.stdout)
            self.assertIn("增量快照", result.stdout)
            self.assertIn("待复核", result.stdout)
            self.assertIn("未推进增量快照", result.stdout)

    def test_scan_distinguishes_all_change_categories_without_leaking_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            paths = {}
            for name in ("metadata", "content", "missing", "unchanged"):
                relative = f"GET NOTES/Notes/2026-09-15/{name}.md"
                paths[name] = write_source(vault, relative, uid=f"uid-{name}", title=name)
            entries = [
                snapshot_entry(paths[name], vault, uid=f"uid-{name}", title=name)
                for name in ("metadata", "content", "missing", "unchanged")
            ]
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, entries)

            paths["metadata"].write_text(
                source_text(uid="uid-metadata", title="metadata changed"), encoding="utf-8"
            )
            secret_body = "PRIVATE-BODY-MUST-NOT-APPEAR"
            paths["content"].write_text(
                source_text(uid="uid-content", title="content", body=secret_body), encoding="utf-8"
            )
            paths["missing"].unlink()
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/added.md",
                uid="uid-added",
                title="added",
                body=secret_body,
            )

            result = self.run_scan(vault, snapshot)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "changes_detected")
            self.assertEqual(
                report["counts"],
                {
                    "added": 1,
                    "index_metadata_changed": 1,
                    "content_only_changed": 1,
                    "missing": 1,
                    "unchanged": 1,
                },
            )
            self.assertEqual(
                {change["kind"] for change in report["changes"]},
                {"added", "index_metadata_changed", "content_only_changed", "missing"},
            )
            self.assertNotIn(secret_body, result.stdout)
            self.assertNotIn(secret_body, result.stderr)

    def test_source_tags_only_change_is_index_metadata_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            source.write_text(
                source_text(uid="uid-a", title="A", tags=("topic-b",)),
                encoding="utf-8",
            )

            result = self.run_scan(vault, snapshot)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["counts"]["index_metadata_changed"], 1)
            self.assertEqual(report["counts"]["content_only_changed"], 0)
            self.assertNotIn("topic-a", result.stdout)
            self.assertNotIn("topic-b", result.stdout)

    def test_invalid_current_index_fields_fail_without_leaking_values(self) -> None:
        invalid_cases = {
            "uid": {"uid": "PRIVATE UID VALUE", "title": "A"},
            "note_type": {
                "uid": "uid-a",
                "title": "A",
                "note_type": "PRIVATE-NOTE-TYPE",
            },
            "created": {"uid": "uid-a", "title": "A", "created": "2026-99-99"},
        }
        for field, source_values in invalid_cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                relative = "GET NOTES/Notes/2026-09-15/stable.md"
                source = write_source(vault, relative, uid="uid-stable", title="stable")
                snapshot = root / "accepted-snapshot.json"
                write_snapshot(
                    snapshot,
                    [snapshot_entry(source, vault, uid="uid-stable", title="stable")],
                )
                write_source(
                    vault,
                    "GET NOTES/Notes/2026-09-15/invalid.md",
                    **source_values,
                )

                result = self.run_scan(vault, snapshot)

                self.assertNotEqual(result.returncode, 0)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "error")
                self.assertNotIn("PRIVATE", result.stdout)
                self.assertNotIn("PRIVATE", result.stderr)

    def test_snapshot_rejects_extra_fields_without_leaking_values(self) -> None:
        for location in ("top_level", "source_entry"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                relative = "GET NOTES/Notes/2026-09-15/a.md"
                source = write_source(vault, relative, uid="uid-a", title="A")
                snapshot = root / "accepted-snapshot.json"
                write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
                payload = json.loads(snapshot.read_text(encoding="utf-8"))
                if location == "top_level":
                    payload["summary"] = "PRIVATE-SUMMARY-MUST-NOT-LEAK"
                else:
                    payload["sources"][0]["summary"] = "PRIVATE-SUMMARY-MUST-NOT-LEAK"
                snapshot.write_text(json.dumps(payload), encoding="utf-8")

                result = self.run_scan(vault, snapshot)

                self.assertNotEqual(result.returncode, 0)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "error")
                self.assertNotIn("PRIVATE-SUMMARY-MUST-NOT-LEAK", result.stdout)
                self.assertNotIn("PRIVATE-SUMMARY-MUST-NOT-LEAK", result.stderr)

    def test_invalid_snapshot_index_fields_fail_without_leaking_values(self) -> None:
        invalid_values = {
            "uid": "PRIVATE UID VALUE",
            "note_type": "PRIVATE-NOTE-TYPE",
            "created_date": "2026-99-99",
        }
        for field, invalid_value in invalid_values.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                relative = "GET NOTES/Notes/2026-09-15/a.md"
                source = write_source(vault, relative, uid="uid-a", title="A")
                snapshot = root / "accepted-snapshot.json"
                entry = snapshot_entry(source, vault, uid="uid-a", title="A")
                entry[field] = invalid_value
                write_snapshot(snapshot, [entry])

                result = self.run_scan(vault, snapshot)

                self.assertNotEqual(result.returncode, 0)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "error")
                self.assertNotIn("PRIVATE", result.stdout)
                self.assertNotIn("PRIVATE", result.stderr)

    def test_special_markdown_file_fails_closed_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            os.mkfifo(vault / "GET NOTES/Notes/2026-09-15/block.md")

            result = self.run_scan(vault, snapshot)

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("regular file" in error for error in report["errors"]))

    def test_source_changed_after_its_scan_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            original_stat_signature = incremental.stat_signature
            call_count = 0

            def mutate_before_final_check(path: Path, display_path: str) -> tuple[int, ...]:
                nonlocal call_count
                call_count += 1
                if call_count == 3:
                    source.write_text(
                        source_text(uid="uid-a", title="A", body="changed after scan"),
                        encoding="utf-8",
                    )
                return original_stat_signature(path, display_path)

            with mock.patch.object(
                incremental,
                "stat_signature",
                side_effect=mutate_before_final_check,
            ):
                with self.assertRaisesRegex(incremental.ScanError, "changed"):
                    incremental.scan_current_sources(vault)

    def test_malformed_frontmatter_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            stable = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/stable.md",
                uid="uid-stable",
                title="stable",
            )
            path = vault / "GET NOTES/Notes/2026-09-15/broken.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not frontmatter\nPRIVATE", encoding="utf-8")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(stable, vault, uid="uid-stable", title="stable")])

            result = self.run_scan(vault, snapshot)

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertIn("frontmatter", report["errors"][0])
            self.assertNotIn("PRIVATE", result.stdout)

    def test_unparseable_frontmatter_line_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            source.write_text(
                source_text(uid="uid-a", title="A").replace(
                    'title: "A"',
                    'title: "A"\nPRIVATE FRONTMATTER GARBAGE',
                ),
                encoding="utf-8",
            )

            result = self.run_scan(vault, snapshot)

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertNotIn("PRIVATE", result.stdout)
            self.assertNotIn("PRIVATE", result.stderr)

    def test_duplicate_current_uid_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            first = write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/a.md",
                uid="same-uid",
                title="A",
            )
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-15/b.md",
                uid="same-uid",
                title="B",
            )
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(first, vault, uid="same-uid", title="A")])

            result = self.run_scan(vault, snapshot)

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("duplicate UID" in error for error in report["errors"]))

    def test_path_or_uid_identity_drift_fails_closed(self) -> None:
        for change_kind in ("path", "uid"):
            with self.subTest(change_kind=change_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                vault = root / "vault"
                relative = "GET NOTES/Notes/2026-09-15/a.md"
                source = write_source(vault, relative, uid="uid-a", title="A")
                snapshot = root / "accepted-snapshot.json"
                write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
                if change_kind == "path":
                    moved = source.with_name("moved.md")
                    source.rename(moved)
                else:
                    source.write_text(source_text(uid="uid-b", title="A"), encoding="utf-8")

                result = self.run_scan(vault, snapshot)

                self.assertNotEqual(result.returncode, 0)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "error")
                self.assertTrue(any("identity" in error for error in report["errors"]))

    def test_symlinked_source_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            external = root / "external"
            external.mkdir()
            (external / "hidden.md").write_text(source_text(uid="uid-hidden", title="hidden"), encoding="utf-8")
            (vault / "GET NOTES/Notes/linked").symlink_to(external, target_is_directory=True)

            result = self.run_scan(vault, snapshot)

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("symlink" in error for error in report["errors"]))

    def test_empty_snapshot_and_empty_source_tree_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            (vault / "GET NOTES/Notes").mkdir(parents=True)
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [])

            result = self.run_scan(vault, snapshot)

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")

    def test_duplicate_required_frontmatter_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            relative = "GET NOTES/Notes/2026-09-15/a.md"
            source = write_source(vault, relative, uid="uid-a", title="A")
            snapshot = root / "accepted-snapshot.json"
            write_snapshot(snapshot, [snapshot_entry(source, vault, uid="uid-a", title="A")])
            source.write_text(
                source_text(uid="uid-a", title="A").replace('title: "A"', 'title: "A"\ntitle: "B"'),
                encoding="utf-8",
            )

            result = self.run_scan(vault, snapshot)

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("duplicate frontmatter key" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
