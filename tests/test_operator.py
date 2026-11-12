from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import get_index_operator as operator
import get_index_incremental as incremental

from get_index_render import INDEX_PAGE_FILENAMES, render_index_pages
from tests.test_incremental_scan import snapshot_entry, write_snapshot, write_source
from tests.test_review_drafts import canonical_record, write_records


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "get_index_operator.py"
COMMAND = PROJECT_ROOT / "运行 GET 索引维护.command"


def prepare_operator_case(
    root: Path,
) -> tuple[Path, Path, list[Path], list[Path], Path]:
    vault = root / "vault"
    stable = write_source(
        vault,
        "GET NOTES/Notes/2026-09-15/stable.md",
        uid="uid-stable",
        title="stable",
    )
    snapshot = root / "state" / "accepted-snapshot.json"
    snapshot.parent.mkdir(parents=True)
    write_snapshot(
        snapshot,
        [snapshot_entry(stable, vault, uid="uid-stable", title="stable")],
    )
    records = [root / f"part-{index}.jsonl" for index in range(1, 4)]
    stable_record = canonical_record(
        stable, vault, uid="uid-stable", title="stable"
    )
    write_records(records[0], [stable_record])
    write_records(records[1], [])
    write_records(records[2], [])
    index_directory = vault / "系统" / "知识库索引"
    index_directory.mkdir(parents=True)
    pages = [index_directory / filename for filename in INDEX_PAGE_FILENAMES]
    rendered = render_index_pages([stable_record])
    for page in pages:
        page.write_text(rendered[page.name], encoding="utf-8")
    runtime = root / "runtime"
    return vault, snapshot, records, pages, runtime


def manifest(paths: list[Path]) -> dict[Path, tuple[bytes, int]]:
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def run_operator(
    vault: Path,
    snapshot: Path,
    records: list[Path],
    runtime: Path,
    *arguments: str,
    input_text: str = "",
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--vault-root",
            str(vault),
            "--snapshot",
            str(snapshot),
            "--records",
            *[str(path) for path in records],
            "--runtime-root",
            str(runtime),
            "--format",
            "json",
            *arguments,
        ],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
        timeout=10,
    )


class OperatorRendererTests(unittest.TestCase):
    def test_change_description_lists_each_nonzero_category(self) -> None:
        self.assertEqual(
            operator.describe_changes(
                {
                    "added": 2,
                    "index_metadata_changed": 1,
                    "content_only_changed": 3,
                    "missing": 0,
                    "unchanged": 5,
                }
            ),
            "新增 2，索引元数据变化 1，仅正文变化 3。",
        )

    def test_human_output_distinguishes_all_operator_states(self) -> None:
        cases = {
            "no_changes": (
                "无变化",
                "当前 GET 来源与已接受快照一致。",
                "未更新规范记录、覆盖索引或增量快照。",
                "无需处理。",
            ),
            "review_required": (
                "需要复核",
                "发现 1 条需要复核的 GET 变化。",
                "只保存了本地复核包；未更新九个受控目标。",
                "按编号完成复核，或稍后重新运行。",
            ),
            "ready_to_apply": (
                "可以应用",
                "复核结果已通过完整预检。",
                "复核结果已保存，但未更新规范记录、覆盖索引或增量快照。",
                "再次运行并输入“应用”才会写入。",
            ),
            "apply_rejected": (
                "拒绝应用",
                "待复核包已失效。",
                "未推进增量快照。",
                "请不要手改 JSON，将本结果交给 Codex 处理。",
            ),
            "applied": (
                "已应用",
                "本次受控更新已完成。",
                "已更新 7 个受控文件；GET 来源保持只读。",
                "无需处理。",
            ),
        }

        for status, expected_lines in cases.items():
            with self.subTest(status=status):
                result = {
                    "status": status,
                    "status_zh": expected_lines[0],
                    "finding": expected_lines[1],
                    "write_summary": expected_lines[2],
                    "next_action": expected_lines[3],
                }
                self.assertEqual(
                    operator.render_human(result),
                    "\n".join(
                        (
                            f"状态：{expected_lines[0]}",
                            f"发现：{expected_lines[1]}",
                            f"写入：{expected_lines[2]}",
                            f"下一步：{expected_lines[3]}",
                        )
                    ),
                )


class OperatorCliTests(unittest.TestCase):
    def assert_packet_ancestor_race_rejected(
        self, operation: str, timing: str, destination: str,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="packet-race-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            stable = next((vault / "GET NOTES").rglob("*.md"))
            if operation == "keep_governance":
                stable.write_text(stable.read_text().replace('title: "stable"',
                                                            'title: "renamed"'))
            else:
                write_source(vault, "GET NOTES/Notes/2026-09-16/new.md",
                             uid="uid-new", title="synthetic new")
            config = operator.OperationConfig(vault, snapshot, tuple(records), runtime)
            report = operator.scan_read_only(config)
            bundle = operator.build_review_bundle(config, report)
            packet_id = operator.packet_id_for(bundle)
            target_parent = vault / "GET NOTES" / "packets" / packet_id
            target_parent.mkdir(parents=True)
            # Existing targets must not be overwritten; no new or temporary
            # source-layer files may be created, even if later cleaned up.
            for name in ("scan.json", "draft.json", "reviewed.json", "manifest.json"):
                (target_parent / name).write_bytes(b"SYNTHETIC-SOURCE-KEEP\n")
            packet = None
            if operation in ("review", "keep_governance"):
                packet = operator.create_or_load_packet(config, report)
                # Reloaded packets need the same protection as newly made ones.
                packet = operator.create_or_load_packet(config, report)
            source_root = vault / "GET NOTES"
            source_paths = sorted(source_root.rglob("*"))
            protected = [p for p in source_paths if p.is_file()] + [snapshot, *records, *pages]
            before = manifest(protected)
            source_names = [p.relative_to(source_root) for p in source_paths]
            moved = root / "runtime-before-swap"
            swapped = False
            source_creations = []
            real_write = incremental.atomic_write_json
            real_open = os.open
            real_fsync = os.fsync
            writing_target = False

            def swap():
                nonlocal swapped
                runtime.rename(moved)
                runtime.symlink_to(source_root if destination == "vault" else moved,
                                   target_is_directory=True)
                swapped = True

            def write_with_race(path, payload, *args, **kwargs):
                nonlocal writing_target
                writing_target = path.name == (operation if operation.endswith(".json")
                                               else "reviewed.json")
                if writing_target and timing == "before_open":
                    swap()
                try:
                    return real_write(path, payload, *args, **kwargs)
                finally:
                    writing_target = False

            def fsync_with_race(fd):
                real_fsync(fd)
                if writing_target and timing == "before_replace" and not swapped:
                    swap()

            def track_open(path, flags, *args, **kwargs):
                parent_fd = kwargs.get("dir_fd")
                if flags & os.O_CREAT and parent_fd is not None:
                    parent = os.fstat(parent_fd)
                    target = target_parent.stat()
                    if (parent.st_dev, parent.st_ino) == (target.st_dev, target.st_ino):
                        source_creations.append(str(path))
                return real_open(path, flags, *args, **kwargs)

            answers = iter(["1"] if operation == "keep_governance" else
                           ["2", "3", "1", "1", "1", "确认复核"])
            rejected = False
            reported_saved = False
            with mock.patch.object(incremental, "atomic_write_json", side_effect=write_with_race), \
                 mock.patch.object(os, "open", side_effect=track_open), \
                 mock.patch.object(os, "fsync", side_effect=fsync_with_race), \
                 mock.patch.object(operator, "prompt_user", side_effect=lambda _msg: next(answers)):
                try:
                    if packet is None:
                        operator.create_or_load_packet(config, report)
                    else:
                        _, reported_saved = operator.review_interactively(packet)
                except incremental.ScanError:
                    rejected = True
            self.assertTrue(swapped, "race injection did not run")
            self.assertEqual(
                {"rejected": rejected, "reported_saved": reported_saved,
                 "source_unchanged": manifest(protected) == before,
                 "source_creations": source_creations},
                {"rejected": True, "reported_saved": False,
                 "source_unchanged": True, "source_creations": []},
            )
            self.assertEqual([p.relative_to(source_root) for p in sorted(source_root.rglob("*"))],
                             source_names)
            self.assertFalse(list(root.rglob(".get-index-draft-*.tmp")))

    def test_packet_directory_creation_ancestor_swap_preserves_get_sources(self) -> None:
        for timing in ("after_runtime_validation", "before_mkdir"):
            for destination in ("vault", "original_directory"):
                with self.subTest(timing=timing, destination=destination):
                    with tempfile.TemporaryDirectory(prefix="mkdir-race-") as temp_dir:
                        root = Path(temp_dir).resolve()
                        vault, snapshot, records, pages, _ = prepare_operator_case(root)
                        write_source(vault, "GET NOTES/Notes/2026-09-16/new.md",
                                     uid="uid-new", title="synthetic new")
                        external = root / "external"
                        external.mkdir()
                        runtime = external / "runtime"
                        moved = root / "external-before-swap"
                        source_root = vault / "GET NOTES"
                        runtime.mkdir(mode=0o700)
                        (source_root / "runtime").mkdir(mode=0o700)
                        protected = sorted(source_root.rglob("*.md")) + [snapshot, *records, *pages]
                        before = manifest(protected)
                        source_names = sorted(p.relative_to(source_root) for p in source_root.rglob("*"))
                        config = operator.OperationConfig(vault, snapshot, tuple(records), runtime)
                        report = operator.scan_read_only(config)
                        real_canonical = operator.canonical_runtime_root
                        real_mkdir = os.mkdir
                        real_path_mkdir = Path.mkdir
                        swapped = False

                        def swap():
                            nonlocal swapped
                            external.rename(moved)
                            external.symlink_to(source_root if destination == "vault" else moved,
                                                target_is_directory=True)
                            swapped = True

                        def canonical_with_race(*args, **kwargs):
                            result = real_canonical(*args, **kwargs)
                            if timing == "after_runtime_validation":
                                swap()
                            return result

                        def mkdir_with_race(path, *args, **kwargs):
                            if timing == "before_mkdir" and not swapped:
                                swap()
                            return real_mkdir(path, *args, **kwargs)

                        def path_mkdir_with_race(path, *args, **kwargs):
                            if timing == "before_mkdir" and not swapped:
                                swap()
                            return real_path_mkdir(path, *args, **kwargs)

                        rejected = False
                        with mock.patch.object(operator, "canonical_runtime_root", side_effect=canonical_with_race), \
                             mock.patch.object(os, "mkdir", side_effect=mkdir_with_race), \
                             mock.patch.object(Path, "mkdir", new=path_mkdir_with_race):
                            try:
                                operator.create_or_load_packet(config, report)
                            except incremental.ScanError:
                                rejected = True
                        self.assertTrue(swapped, "race injection did not run")
                        self.assertEqual(
                            {"rejected": rejected, "source_unchanged": manifest(protected) == before,
                             "source_names": sorted(p.relative_to(source_root) for p in source_root.rglob("*"))},
                            {"rejected": True, "source_unchanged": True, "source_names": source_names},
                        )

    def test_packet_creation_ancestor_swap_preserves_get_sources(self) -> None:
        for name in ("scan.json", "draft.json", "reviewed.json", "manifest.json"):
            for timing in ("before_open", "before_replace"):
                for destination in ("vault", "original_directory"):
                    with self.subTest(name=name, timing=timing, destination=destination):
                        self.assert_packet_ancestor_race_rejected(name, timing, destination)

    def test_review_save_ancestor_swap_preserves_get_sources(self) -> None:
        for operation in ("review", "keep_governance"):
            for timing in ("before_open", "before_replace"):
                for destination in ("vault", "original_directory"):
                    with self.subTest(operation=operation, timing=timing, destination=destination):
                        self.assert_packet_ancestor_race_rejected(operation, timing, destination)

    def test_command_reports_four_lines_when_python3_is_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="无 Python ") as temp_dir:
            result = subprocess.run(
                [str(COMMAND), "--check-only"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
                env={**os.environ, "PATH": temp_dir},
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr.splitlines(),
            [
                "状态：拒绝应用",
                "发现：未找到 Python 3。",
                "写入：未写入运行文件、GET 来源或九个受控目标。",
                "下一步：请将本结果交给 Codex 处理。",
            ],
        )

    def test_check_only_refuses_pending_transaction_without_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            journal = snapshot.parent / "apply-transaction.json"
            journal.write_text("{}\n", encoding="utf-8")
            protected = [
                *sorted(vault.rglob("*.md")),
                snapshot,
                *records,
                *pages,
                journal,
            ]
            before = manifest(protected)

            result = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                "--check-only",
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "apply_rejected")
            self.assertEqual(report["reason_code"], "pending_transaction_read_only")
            self.assertEqual(manifest(protected), before)
            self.assertFalse(runtime.exists())

    def test_broken_persisted_index_is_rejected_read_only_before_no_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            pages[3].write_text("损坏的索引页\n", encoding="utf-8")
            protected = [
                *sorted(vault.rglob("*.md")),
                snapshot,
                *records,
                *pages,
            ]
            before = manifest(protected)

            result = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                "--check-only",
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "apply_rejected")
            self.assertFalse(report["runtime_artifact_written"])
            self.assertFalse(report["managed_write_performed"])
            self.assertEqual(manifest(protected), before)
            self.assertFalse(runtime.exists())

    def test_command_runs_from_an_unrelated_directory_and_forwards_arguments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, _pages, runtime = prepare_operator_case(root)
            unrelated = root / "unrelated cwd"
            unrelated.mkdir()

            result = subprocess.run(
                [
                    str(COMMAND),
                    "--vault-root",
                    str(vault),
                    "--snapshot",
                    str(snapshot),
                    "--records",
                    *[str(path) for path in records],
                    "--runtime-root",
                    str(runtime),
                    "--check-only",
                    "--format",
                    "json",
                ],
                text=True,
                capture_output=True,
                check=False,
                cwd=unrelated,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "no_changes")
            self.assertFalse(runtime.exists())

    def test_check_only_reports_changes_without_writing_any_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            added = write_source(
                vault,
                "GET NOTES/Notes/2026-09-16/new.md",
                uid="uid-new",
                title="new",
            )
            runtime.mkdir()
            runtime_marker = runtime / "keep.txt"
            runtime_marker.write_text("KEEP\n", encoding="utf-8")
            protected = [
                *[path for path in vault.rglob("*.md")],
                snapshot,
                *records,
                *pages,
                runtime_marker,
            ]
            before = manifest(protected)

            result = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                "--check-only",
                cwd=root,
            )

            self.assertEqual(result.returncode, 10, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "review_required")
            self.assertEqual(report["status_zh"], "需要复核")
            self.assertEqual(report["counts"]["added"], 1)
            self.assertFalse(report["managed_write_performed"])
            self.assertFalse(report["runtime_artifact_written"])
            self.assertEqual(manifest(protected), before)
            self.assertEqual(set(runtime.iterdir()), {runtime_marker})
            self.assertIn(added, protected)

    def test_added_source_can_be_reviewed_cancelled_reused_applied_and_rechecked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-16/new.md",
                uid="uid-new",
                title="new title",
            )
            get_sources = sorted((vault / "GET NOTES" / "Notes").rglob("*.md"))
            managed = [snapshot, *records, *pages]
            get_before = manifest(get_sources)
            managed_before = manifest(managed)

            detected = run_operator(vault, snapshot, records, runtime)

            self.assertEqual(detected.returncode, 10, detected.stderr)
            detected_report = json.loads(detected.stdout)
            self.assertEqual(detected_report["status"], "review_required")
            self.assertTrue(detected_report["runtime_artifact_written"])
            self.assertFalse(detected_report["managed_write_performed"])
            packet_directories = list((runtime / "packets").iterdir())
            self.assertEqual(len(packet_directories), 1)
            packet = packet_directories[0]
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertEqual((runtime / "packets").stat().st_mode & 0o777, 0o700)
            self.assertEqual(packet.stat().st_mode & 0o777, 0o700)
            draft = packet / "draft.json"
            reviewed = packet / "reviewed.json"
            self.assertTrue(draft.is_file())
            self.assertTrue(reviewed.is_file())
            self.assertEqual(draft.stat().st_mode & 0o777, 0o600)
            draft_before = (draft.read_bytes(), draft.stat().st_mtime_ns)
            self.assertEqual(manifest(get_sources), get_before)
            self.assertEqual(manifest(managed), managed_before)

            cancelled = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                input_text="2\n3\n1,3\n1\n2\n确认复核\n取消\n",
            )

            self.assertEqual(cancelled.returncode, 11, cancelled.stderr)
            self.assertIn("风险标记", cancelled.stderr)
            self.assertIn("无", cancelled.stderr)
            self.assertIn("总结缺失", cancelled.stderr)
            self.assertNotIn("hr", cancelled.stderr)
            self.assertNotIn("summary_missing", cancelled.stderr)
            cancelled_report = json.loads(cancelled.stdout)
            self.assertEqual(cancelled_report["status"], "ready_to_apply")
            self.assertEqual(cancelled_report["reason_code"], "user_cancelled")
            self.assertTrue(cancelled_report["review_decision_saved"])
            self.assertFalse(cancelled_report["managed_write_performed"])
            self.assertEqual(
                cancelled_report["write_summary"],
                "复核结果已保存，但未更新规范记录、覆盖索引或增量快照。",
            )
            self.assertEqual(
                (draft.read_bytes(), draft.stat().st_mtime_ns), draft_before
            )
            reviewed_after_cancel = (
                reviewed.read_bytes(),
                reviewed.stat().st_mtime_ns,
            )
            reviewed_payload = json.loads(reviewed.read_text(encoding="utf-8"))
            self.assertEqual(reviewed_payload["drafts"][0]["basis"], "summary")
            self.assertEqual(
                reviewed_payload["drafts"][0]["review_state"], "已核验"
            )
            self.assertEqual(manifest(get_sources), get_before)
            self.assertEqual(manifest(managed), managed_before)

            applied = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                input_text="应用\n",
            )

            self.assertEqual(applied.returncode, 0, applied.stderr)
            applied_report = json.loads(applied.stdout)
            self.assertEqual(applied_report["status"], "applied")
            self.assertTrue(applied_report["managed_write_performed"])
            self.assertGreater(applied_report["managed_changed_file_count"], 0)
            self.assertEqual(
                (draft.read_bytes(), draft.stat().st_mtime_ns), draft_before
            )
            self.assertEqual(
                (reviewed.read_bytes(), reviewed.stat().st_mtime_ns),
                reviewed_after_cancel,
            )
            self.assertEqual(manifest(get_sources), get_before)
            self.assertFalse((snapshot.parent / "apply-transaction.json").exists())
            managed_after_apply = manifest(managed)

            repeated = run_operator(vault, snapshot, records, runtime)

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            repeated_report = json.loads(repeated.stdout)
            self.assertEqual(repeated_report["status"], "no_changes")
            self.assertFalse(repeated_report["runtime_artifact_written"])
            self.assertEqual(manifest(get_sources), get_before)
            self.assertEqual(manifest(managed), managed_after_apply)

    def test_multiple_review_decisions_are_all_saved_in_one_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            for index in (1, 2):
                write_source(
                    vault,
                    f"GET NOTES/Notes/2026-09-16/new-{index}.md",
                    uid=f"uid-new-{index}",
                    title=f"new title {index}",
                )
            get_sources = sorted((vault / "GET NOTES" / "Notes").rglob("*.md"))
            get_before = manifest(get_sources)
            managed = [snapshot, *records, *pages]
            managed_before = manifest(managed)

            result = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                input_text=(
                    "2\n1\n3\n1\n2\n确认复核\n"
                    "3\n3\n1\n2\n2\n确认复核\n"
                    "取消\n"
                ),
            )

            self.assertEqual(result.returncode, 11, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "ready_to_apply")
            reviewed_path = next((runtime / "packets").iterdir()) / "reviewed.json"
            drafts = json.loads(reviewed_path.read_text(encoding="utf-8"))["drafts"]
            self.assertEqual([draft["review_state"] for draft in drafts], ["已核验"] * 2)
            self.assertEqual([draft["domain"] for draft in drafts], ["工作/个人", "工作/团队"])
            self.assertEqual([draft["sensitivity"] for draft in drafts], ["受限", "普通"])
            self.assertEqual([draft["tags"] for draft in drafts], [["项目与流程"], ["AI与自动化"]])
            self.assertEqual(manifest(get_sources), get_before)
            self.assertEqual(manifest(managed), managed_before)

    def test_apply_rechecks_ready_state_and_rejects_stale_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            added = write_source(
                vault,
                "GET NOTES/Notes/2026-09-16/new.md",
                uid="uid-new",
                title="new title",
            )
            first = run_operator(vault, snapshot, records, runtime)
            self.assertEqual(first.returncode, 10, first.stderr)
            packet = next((runtime / "packets").iterdir())
            packet_files = list(packet.iterdir())
            packet_before = manifest(packet_files)
            managed = [snapshot, *records, *pages]
            managed_before = manifest(managed)
            command = [
                sys.executable,
                str(CLI),
                "--vault-root",
                str(vault),
                "--snapshot",
                str(snapshot),
                "--records",
                *[str(path) for path in records],
                "--runtime-root",
                str(runtime),
                "--format",
                "json",
            ]
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=root,
            )
            assert process.stdin is not None
            process.stdin.write("2\n3\n1\n1\n1\n确认复核\n")
            process.stdin.flush()
            assert process.stderr is not None
            prompts: list[str] = []
            while True:
                line = process.stderr.readline()
                self.assertNotEqual(line, "")
                prompts.append(line)
                if "可以应用" in line:
                    break
            added.write_text(added.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
            process.stdin.write("应用\n")
            process.stdin.flush()
            process.stdin.close()
            stdout = process.stdout.read() if process.stdout is not None else ""
            stderr = process.stderr.read()
            process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            process.stderr.close()

            self.assertEqual(process.returncode, 2, stderr)
            report = json.loads(stdout)
            self.assertEqual(report["status"], "apply_rejected")
            self.assertEqual(report["reason_code"], "validation_failed")
            self.assertFalse(report["managed_write_performed"])
            self.assertTrue(report["runtime_artifact_written"])
            self.assertTrue(report["review_decision_saved"])
            self.assertIn("本地复核", report["write_summary"])
            self.assertEqual(manifest(managed), managed_before)
            self.assertNotEqual(manifest(packet_files), packet_before)
            self.assertEqual(list((runtime / "packets").iterdir()), [packet])

    def test_runtime_alias_into_vault_is_rejected_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, _runtime = prepare_operator_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-16/new.md",
                uid="uid-new",
                title="new title",
            )
            runtime_alias = root / "runtime-alias"
            runtime_alias.symlink_to(vault, target_is_directory=True)
            protected = [
                *sorted(vault.rglob("*.md")),
                snapshot,
                *records,
                *pages,
            ]
            before = manifest(protected)

            result = run_operator(
                vault,
                snapshot,
                records,
                runtime_alias,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "apply_rejected")
            self.assertIn("符号链接", report["finding"])
            self.assertFalse(report["runtime_artifact_written"])
            self.assertEqual(manifest(protected), before)
            self.assertEqual(set(vault.iterdir()), {vault / "GET NOTES", vault / "系统"})

    def test_metadata_change_can_keep_existing_governance_without_reselection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            source = next((vault / "GET NOTES" / "Notes").rglob("*.md"))
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    'title: "stable"', 'title: "renamed"'
                ),
                encoding="utf-8",
            )
            managed_before = manifest([snapshot, *records, *pages])

            detected = run_operator(vault, snapshot, records, runtime)
            self.assertEqual(detected.returncode, 10, detected.stderr)
            kept = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                input_text="1\n取消\n",
            )

            self.assertEqual(kept.returncode, 11, kept.stderr)
            report = json.loads(kept.stdout)
            self.assertEqual(report["status"], "ready_to_apply")
            self.assertIn("工作/团队", kept.stderr)
            self.assertIn("内部", kept.stderr)
            self.assertIn("项目与流程", kept.stderr)
            reviewed_path = next((runtime / "packets").iterdir()) / "reviewed.json"
            reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))["drafts"][0]
            self.assertEqual(reviewed["domain"], "工作/团队")
            self.assertEqual(reviewed["sensitivity"], "内部")
            self.assertEqual(reviewed["tags"], ["项目与流程"])
            self.assertEqual(reviewed["flag"], "none")
            self.assertEqual(reviewed["basis"], "metadata")
            self.assertEqual(reviewed["review_state"], "规则初分")
            self.assertEqual(manifest([snapshot, *records, *pages]), managed_before)

    def test_pending_transaction_is_rolled_back_before_scan_and_requires_confirmation_again(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="GET 入口 ") as temp_dir:
            root = Path(temp_dir)
            vault, snapshot, records, pages, runtime = prepare_operator_case(root)
            write_source(
                vault,
                "GET NOTES/Notes/2026-09-16/new.md",
                uid="uid-new",
                title="new title",
            )
            detected = run_operator(vault, snapshot, records, runtime)
            self.assertEqual(detected.returncode, 10, detected.stderr)
            reviewed = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                input_text="2\n1\n3\n1\n1\n确认复核\n取消\n",
            )
            self.assertEqual(reviewed.returncode, 11, reviewed.stderr)
            reviewed_path = next((runtime / "packets").iterdir()) / "reviewed.json"
            managed = [snapshot, *records, *pages]
            before = manifest(managed)
            real_replace = incremental.replace_files_transactionally

            def crash_before_commit_marker(contents, **kwargs):
                def stop_after_first_target(path: Path, _index: int) -> None:
                    if path != kwargs["commit_marker"]:
                        raise SystemExit(71)

                return real_replace(
                    contents,
                    **{**kwargs, "after_replace": stop_after_first_target},
                )

            args = operator.apply_namespace(
                operator.OperationConfig(
                    vault_root=vault,
                    snapshot=snapshot,
                    records=tuple(records),
                    runtime_root=runtime,
                ),
                reviewed_path,
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
            self.assertNotEqual(manifest(managed), before)

            cancelled = run_operator(
                vault,
                snapshot,
                records,
                runtime,
                input_text="取消\n",
            )

            self.assertEqual(cancelled.returncode, 11, cancelled.stderr)
            report = json.loads(cancelled.stdout)
            self.assertEqual(report["status"], "ready_to_apply")
            self.assertEqual(report["reason_code"], "user_cancelled")
            self.assertTrue(report["recovery_performed"])
            self.assertFalse(report["managed_write_performed"])
            self.assertIn("已回滚上次未完成的应用事务", report["write_summary"])
            self.assertEqual(manifest(managed), before)
            self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
