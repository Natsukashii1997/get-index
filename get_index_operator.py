#!/usr/bin/env python3
"""Human-facing, fail-closed entry point for GET index maintenance."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat as stat_module
import sys
from typing import Any

import get_index_incremental as incremental
from get_index_render import (
    INDEX_PAGE_FILENAMES,
    IndexRenderError,
    TOPIC_ORDER,
    validate_index_pages,
)
from get_index_transaction import TransactionError


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_VAULT_ROOT = Path("/Users/nat/Documents/Obsidian Vault")
DEFAULT_SNAPSHOT = PROJECT_ROOT / "state" / "accepted-snapshot.json"
DEFAULT_RECORDS = (
    PROJECT_ROOT / "part-feb-may.jsonl",
    PROJECT_ROOT / "part-jun-jul.jsonl",
    PROJECT_ROOT / "part-aug-sep.jsonl",
)
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "get-index-maintenance"
STATUS_ZH = {
    "no_changes": "无变化",
    "review_required": "需要复核",
    "ready_to_apply": "可以应用",
    "apply_rejected": "拒绝应用",
    "applied": "已应用",
}
EXIT_CODES = {
    "no_changes": 0,
    "review_required": 10,
    "ready_to_apply": 11,
    "apply_rejected": 2,
    "applied": 0,
}
FLAG_CHOICES = (
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
)
FLAG_CHOICE_LABELS = (
    "无",
    "时效",
    "人事",
    "混合",
    "客户",
    "个案",
    "学生",
    "私人",
    "医疗",
    "近似重复",
    "总结缺失",
)
DOMAIN_CHOICES = ("生活", "工作/个人", "工作/团队")
SENSITIVITY_CHOICES = ("受限", "内部", "普通")


@dataclass(frozen=True)
class OperationConfig:
    vault_root: Path
    snapshot: Path
    records: tuple[Path, Path, Path]
    runtime_root: Path


def render_human(result: dict[str, Any]) -> str:
    """Render one stable four-line outcome without exposing machine payloads."""

    return "\n".join(
        (
            f"状态：{result['status_zh']}",
            f"发现：{result['finding']}",
            f"写入：{result['write_summary']}",
            f"下一步：{result['next_action']}",
        )
    )


def result_payload(
    status: str,
    *,
    finding: str,
    write_summary: str,
    next_action: str,
    reason_code: str,
    counts: dict[str, int] | None = None,
    managed_write_performed: bool = False,
    runtime_artifact_written: bool = False,
    review_decision_saved: bool = False,
    managed_changed_file_count: int = 0,
    attention_required: bool = False,
    recovery_performed: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "status_zh": STATUS_ZH[status],
        "reason_code": reason_code,
        "finding": finding,
        "write_summary": write_summary,
        "next_action": next_action,
        "counts": counts,
        "managed_write_performed": managed_write_performed,
        "managed_changed_file_count": managed_changed_file_count,
        "runtime_artifact_written": runtime_artifact_written,
        "review_decision_saved": review_decision_saved,
        "attention_required": attention_required,
        "recovery_performed": recovery_performed,
    }


def scan_read_only(config: OperationConfig) -> dict[str, Any]:
    accepted = incremental.load_snapshot(config.snapshot)
    records = incremental.load_canonical_records(list(config.records))
    incremental.validate_records_match_snapshot(records, accepted)
    index_directory = config.vault_root / "系统" / "知识库索引"
    pages = {
        filename: incremental.read_regular_text(
            index_directory / filename, "覆盖索引页"
        )
        for filename in INDEX_PAGE_FILENAMES
    }
    validate_index_pages(pages, list(records.values()))
    current = incremental.scan_current_sources(config.vault_root)
    return incremental.compare_sources(accepted, current)


def changed_count(report: dict[str, Any]) -> int:
    return sum(
        count for kind, count in report["counts"].items() if kind != "unchanged"
    )


def describe_changes(counts: dict[str, int]) -> str:
    labels = (
        ("added", "新增"),
        ("index_metadata_changed", "索引元数据变化"),
        ("content_only_changed", "仅正文变化"),
        ("missing", "缺失"),
    )
    details = [f"{label} {counts[key]}" for key, label in labels if counts[key]]
    return "，".join(details) + "。"


def check_only(config: OperationConfig) -> dict[str, Any]:
    journal = Path(os.path.abspath(config.snapshot)).parent / "apply-transaction.json"
    try:
        journal.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise incremental.ScanError("遗留应用事务无法检查") from exc
    else:
        return result_payload(
            "apply_rejected",
            finding="检测到未完成的应用事务。",
            write_summary="--check-only 保持严格只读，未执行恢复或其他写入。",
            next_action="请使用普通入口恢复事务，或将本结果交给 Codex 处理。",
            reason_code="pending_transaction_read_only",
        )
    report = scan_read_only(config)
    counts = report["counts"]
    total_changed = changed_count(report)
    if not total_changed:
        return result_payload(
            "no_changes",
            finding="当前 GET 来源与已接受快照一致。",
            write_summary="未更新规范记录、覆盖索引或增量快照。",
            next_action="无需处理。",
            reason_code="sources_unchanged",
            counts=counts,
        )
    if counts["missing"]:
        return result_payload(
            "apply_rejected",
            finding=f"发现变化：{describe_changes(counts)}",
            write_summary="只读检查未写入任何文件，也未推进增量快照。",
            next_action="请不要手改 JSON，将本结果交给 Codex 处理。",
            reason_code="source_missing",
            counts=counts,
        )
    return result_payload(
        "review_required",
        finding=f"发现变化：{describe_changes(counts)}",
        write_summary="只读检查未保存复核包，也未更新九个受控目标。",
        next_action="运行不带 --check-only 的入口以生成复核包。",
        reason_code="changes_detected_read_only",
        counts=counts,
    )


def canonical_runtime_root(config: OperationConfig) -> Path:
    absolute = Path(os.path.abspath(config.runtime_root))
    probe = absolute
    missing_parts: list[str] = []
    while True:
        try:
            metadata = probe.lstat()
            break
        except FileNotFoundError:
            if probe.parent == probe:
                raise incremental.ScanError("运行目录无法安全解析")
            missing_parts.append(probe.name)
            probe = probe.parent
        except OSError as exc:
            raise incremental.ScanError("运行目录无法检查") from exc
    if stat_module.S_ISLNK(metadata.st_mode):
        raise incremental.ScanError("运行目录不得通过符号链接指向其他位置")
    try:
        canonical = probe.resolve(strict=True).joinpath(*reversed(missing_parts))
        vault_root = Path(os.path.abspath(config.vault_root)).resolve(strict=True)
    except OSError as exc:
        raise incremental.ScanError("运行目录无法安全解析") from exc
    if canonical == vault_root or canonical.is_relative_to(vault_root):
        raise incremental.ScanError("运行目录必须位于 Vault 之外")
    return canonical


def secure_mkdir(path: Path) -> None:
    missing: list[str] = []
    probe = path
    while True:
        try:
            metadata = probe.lstat()
            if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISDIR(
                metadata.st_mode
            ):
                raise incremental.ScanError("运行路径必须由真实目录组成")
            break
        except FileNotFoundError:
            if probe.parent == probe:
                raise incremental.ScanError("运行目录无法安全创建")
            missing.append(probe.name)
            probe = probe.parent
        except OSError as exc:
            raise incremental.ScanError("运行目录无法检查") from exc
    for name in reversed(missing):
        probe = probe / name
        try:
            probe.mkdir(mode=0o700)
        except OSError as exc:
            raise incremental.ScanError("运行目录无法安全创建") from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise incremental.ScanError("运行目录创建后无法检查") from exc
    if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISDIR(metadata.st_mode):
        raise incremental.ScanError("运行目录必须是真实目录")
    if metadata.st_mode & 0o777 != 0o700:
        path.chmod(0o700)


def json_digest(payload: dict[str, Any]) -> str:
    content = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def load_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(incremental.read_regular_text(path, label))
    except json.JSONDecodeError as exc:
        raise incremental.ScanError(f"{label}不是有效 JSON") from exc
    if not isinstance(raw, dict):
        raise incremental.ScanError(f"{label}必须是 JSON 对象")
    return raw


@dataclass(frozen=True)
class ReviewPacket:
    packet_id: str
    directory: Path
    draft: Path
    reviewed: Path
    created: bool


def build_review_bundle(
    config: OperationConfig, report: dict[str, Any]
) -> dict[str, Any]:
    accepted = incremental.load_snapshot(config.snapshot)
    current = incremental.scan_current_sources(config.vault_root)
    records = incremental.load_canonical_records(list(config.records))
    incremental.validate_records_match_snapshot(records, accepted)
    return incremental.validate_review_bundle(
        incremental.build_review_bundle(accepted, current, report, records)
    )


def packet_id_for(bundle: dict[str, Any]) -> str:
    material = "|".join(
        bundle[key]
        for key in (
            "accepted_fingerprint",
            "current_fingerprint",
            "records_fingerprint",
        )
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def create_or_load_packet(
    config: OperationConfig,
    report: dict[str, Any],
) -> ReviewPacket:
    runtime_root = canonical_runtime_root(config)
    bundle = build_review_bundle(config, report)
    packet_id = packet_id_for(bundle)
    packets_root = runtime_root / "packets"
    packet_directory = packets_root / packet_id
    draft = packet_directory / "draft.json"
    reviewed = packet_directory / "reviewed.json"
    manifest = packet_directory / "manifest.json"
    secure_mkdir(runtime_root)
    secure_mkdir(packets_root)
    if packet_directory.exists():
        metadata = packet_directory.lstat()
        if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISDIR(
            metadata.st_mode
        ):
            raise incremental.ScanError("复核包目录不安全")
        stored_manifest = load_json_file(manifest, "复核包清单")
        expected_manifest = {
            "schema_version": 1,
            "packet_id": packet_id,
            "draft_sha256": json_digest(bundle),
        }
        if stored_manifest != expected_manifest:
            raise incremental.ScanError("已有复核包与当前变化不一致")
        stored_draft = incremental.load_review_bundle(draft)
        if stored_draft != bundle:
            raise incremental.ScanError("原始复核草稿已被改动")
        incremental.load_review_bundle(reviewed)
        return ReviewPacket(packet_id, packet_directory, draft, reviewed, False)

    secure_mkdir(packet_directory)
    incremental.atomic_write_json(packet_directory / "scan.json", report)
    incremental.atomic_write_json(draft, bundle)
    incremental.atomic_write_json(reviewed, bundle)
    incremental.atomic_write_json(
        manifest,
        {
            "schema_version": 1,
            "packet_id": packet_id,
            "draft_sha256": json_digest(bundle),
        },
    )
    return ReviewPacket(packet_id, packet_directory, draft, reviewed, True)


def prompt_user(message: str) -> str:
    print(message, file=sys.stderr, flush=True)
    return sys.stdin.readline().strip()


def numbered_choice(
    title: str,
    choices: tuple[str, ...],
    labels: tuple[str, ...] | None = None,
) -> str | None:
    visible = labels if labels is not None else choices
    rendered = "  ".join(
        f"{index}={label}" for index, label in enumerate(visible, start=1)
    )
    value = prompt_user(f"{title}\n{rendered}\n输入编号（0=稍后处理）：")
    if not value:
        return None
    try:
        index = int(value)
    except ValueError:
        return None
    if not 1 <= index <= len(choices):
        return None
    return choices[index - 1]


def topic_choice() -> list[str] | None:
    rendered = "  ".join(
        f"{index}={choice}" for index, choice in enumerate(TOPIC_ORDER, start=1)
    )
    value = prompt_user(
        f"稳定主题（选 1–2 个）\n{rendered}\n输入编号，用逗号分隔（0=稍后处理）："
    )
    try:
        indexes = [int(part.strip()) for part in value.split(",")]
    except ValueError:
        return None
    if not 1 <= len(indexes) <= 2 or len(set(indexes)) != len(indexes):
        return None
    if any(not 1 <= index <= len(TOPIC_ORDER) for index in indexes):
        return None
    return [TOPIC_ORDER[index - 1] for index in indexes]


def review_interactively(packet: ReviewPacket) -> tuple[dict[str, Any], bool]:
    bundle = incremental.load_review_bundle(packet.reviewed)
    saved = False
    for index in range(len(bundle["drafts"])):
        draft = bundle["drafts"][index]
        if not draft["unresolved_fields"] and draft["review_state"] != "待复核":
            continue
        print(
            f"待复核：{draft['title']}（{draft['created_date']}）",
            file=sys.stderr,
        )
        if draft["change_kind"] == "index_metadata_changed":
            flag_label = FLAG_CHOICE_LABELS[FLAG_CHOICES.index(draft["flag"])]
            print(
                "既有治理："
                f"领域={draft['domain']}；敏感性={draft['sensitivity']}；"
                f"稳定主题={'、'.join(draft['tags'])}；风险={flag_label}",
                file=sys.stderr,
            )
            keep_choice = numbered_choice(
                "是否沿用既有治理",
                ("沿用既有治理", "重新选择"),
            )
            if keep_choice is None:
                return bundle, saved
            if keep_choice == "沿用既有治理":
                draft["review_state"] = (
                    "已核验" if draft["basis"] == "summary" else "规则初分"
                )
                bundle = incremental.validate_review_bundle(bundle)
                incremental.atomic_write_json(packet.reviewed, bundle)
                saved = True
                continue
        domain = numbered_choice("领域", DOMAIN_CHOICES)
        if domain is None:
            return bundle, saved
        sensitivity = numbered_choice("敏感性（不确定时保持“受限”）", SENSITIVITY_CHOICES)
        if sensitivity is None:
            return bundle, saved
        tags = topic_choice()
        if tags is None:
            return bundle, saved
        flag = numbered_choice("风险标记", FLAG_CHOICES, FLAG_CHOICE_LABELS)
        if flag is None:
            return bundle, saved
        basis_label = numbered_choice(
            "判断依据",
            ("仅核对标题、日期、类型等元数据", "已核对 GET 总结"),
        )
        if basis_label is None:
            return bundle, saved
        basis = "metadata" if basis_label.startswith("仅核对") else "summary"
        review_state = "规则初分" if basis == "metadata" else "已核验"
        if prompt_user("输入“确认复核”保存这条决定：") != "确认复核":
            return bundle, saved
        draft.update(
            {
                "domain": domain,
                "sensitivity": sensitivity,
                "tags": tags,
                "review_state": review_state,
                "basis": basis,
                "flag": flag,
                "unresolved_fields": [],
            }
        )
        bundle = incremental.validate_review_bundle(bundle)
        incremental.atomic_write_json(packet.reviewed, bundle)
        saved = True
    return bundle, saved


def apply_namespace(config: OperationConfig, review_draft: Path) -> argparse.Namespace:
    return argparse.Namespace(
        vault_root=config.vault_root,
        snapshot=config.snapshot,
        review_draft=review_draft,
        records=list(config.records),
    )


def recovery_namespace(config: OperationConfig) -> argparse.Namespace:
    return argparse.Namespace(
        vault_root=config.vault_root,
        snapshot=config.snapshot,
        records=list(config.records),
    )


def review_is_complete(bundle: dict[str, Any]) -> bool:
    return all(
        not draft["unresolved_fields"] and draft["review_state"] != "待复核"
        for draft in bundle["drafts"]
    )


def normal_run(config: OperationConfig) -> dict[str, Any]:
    recovery = incremental.recover_apply_transaction(recovery_namespace(config))
    if recovery["status"] == "applied_recovered":
        attention = recovery.get("pending_change_count", 0) > 0
        return result_payload(
            "applied",
            finding="上次受控更新已在中断前完成提交。",
            write_summary="已验证提交结果并清理遗留事务；GET 来源保持只读。",
            next_action="请重新运行只读检查。" if attention else "无需处理。",
            reason_code="applied_recovered",
            managed_changed_file_count=0,
            attention_required=attention,
            recovery_performed=True,
        )
    recovery_performed = recovery["status"] == "rolled_back"

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        result["recovery_performed"] = recovery_performed
        if recovery_performed:
            result["write_summary"] = (
                "已回滚上次未完成的应用事务；" + result["write_summary"]
            )
        return result

    report = scan_read_only(config)
    counts = report["counts"]
    total_changed = changed_count(report)
    if not total_changed:
        return finish(check_only(config))
    if counts["missing"]:
        return finish(check_only(config))

    packet = create_or_load_packet(config, report)
    bundle, review_saved = review_interactively(packet)
    if not review_is_complete(bundle):
        return finish(result_payload(
            "review_required",
            finding=f"发现变化：{describe_changes(counts)}",
            write_summary="只保存了本地复核包；未更新九个受控目标。",
            next_action="按编号完成复核，或稍后重新运行。",
            reason_code="review_incomplete",
            counts=counts,
            runtime_artifact_written=packet.created or review_saved,
            review_decision_saved=review_saved,
        ))

    def reject_after_packet(exc: Exception) -> dict[str, Any]:
        runtime_written = packet.created or review_saved
        write_summary = (
            "本地复核包或复核决定已保存；"
            "九个受控目标未更新，增量快照未推进。"
            if runtime_written
            else "未更新九个受控目标，也未推进增量快照。"
        )
        message = str(exc).splitlines()[0] if str(exc).splitlines() else exc.__class__.__name__
        return finish(result_payload(
            "apply_rejected",
            finding=message,
            write_summary=write_summary,
            next_action="请不要手改 JSON，将本结果交给 Codex 处理。",
            reason_code="validation_failed",
            counts=counts,
            runtime_artifact_written=runtime_written,
            review_decision_saved=review_saved,
        ))

    try:
        preflight = incremental.preflight_review_draft(
            apply_namespace(config, packet.reviewed)
        )
    except (incremental.ScanError, IndexRenderError, TransactionError) as exc:
        return reject_after_packet(exc)
    if preflight["status"] == "no_changes":
        return finish(result_payload(
            "no_changes",
            finding="当前 GET 来源与已接受快照一致。",
            write_summary="未更新规范记录、覆盖索引或增量快照。",
            next_action="无需处理。",
            reason_code="already_applied" if preflight.get("already_applied") else "sources_unchanged",
            counts=counts,
            runtime_artifact_written=review_saved,
            review_decision_saved=review_saved,
        ))
    if prompt_user("状态：可以应用。输入“应用”才会更新索引：") != "应用":
        return finish(result_payload(
            "ready_to_apply",
            finding=f"发现变化：{describe_changes(counts)}复核结果已通过完整预检。",
            write_summary="复核结果已保存，但未更新规范记录、覆盖索引或增量快照。",
            next_action="再次运行并输入“应用”才会写入。",
            reason_code="user_cancelled",
            counts=counts,
            runtime_artifact_written=packet.created or review_saved,
            review_decision_saved=review_saved,
        ))

    try:
        applied = incremental.apply_review_draft(
            apply_namespace(config, packet.reviewed)
        )
    except (incremental.ScanError, IndexRenderError, TransactionError) as exc:
        return reject_after_packet(exc)
    if applied["status"] == "no_changes":
        return finish(result_payload(
            "no_changes",
            finding="当前 GET 来源与已接受快照一致。",
            write_summary="未更新规范记录、覆盖索引或增量快照。",
            next_action="无需处理。",
            reason_code="already_applied",
            counts=counts,
        ))
    attention = applied["status"] in {
        "applied_with_new_changes",
        "applied_check_required",
    }
    return finish(result_payload(
        "applied",
        finding=f"发现变化：{describe_changes(counts)}本次受控更新已完成。",
        write_summary=(
            f"已更新 {applied['changed_file_count']} 个受控文件；"
            "GET 来源保持只读。"
        ),
        next_action="请重新运行只读检查。" if attention else "无需处理。",
        reason_code=applied["status"],
        counts=counts,
        managed_write_performed=applied["changed_file_count"] > 0,
        managed_changed_file_count=applied["changed_file_count"],
        runtime_artifact_written=packet.created or review_saved,
        review_decision_saved=review_saved,
        attention_required=attention,
    ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--records",
        type=Path,
        nargs=3,
        default=list(DEFAULT_RECORDS),
        metavar=("RECORD_1", "RECORD_2", "RECORD_3"),
    )
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--check-only", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> OperationConfig:
    return OperationConfig(
        vault_root=args.vault_root,
        snapshot=args.snapshot,
        records=tuple(args.records),
        runtime_root=args.runtime_root,
    )


def rejected_from_exception(exc: Exception) -> dict[str, Any]:
    message = str(exc).splitlines()[0] if str(exc).splitlines() else exc.__class__.__name__
    return result_payload(
        "apply_rejected",
        finding=message,
        write_summary="未推进增量快照。",
        next_action="请不要手改 JSON，将本结果交给 Codex 处理。",
        reason_code="validation_failed",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)
    try:
        if args.check_only:
            result = check_only(config)
        else:
            result = normal_run(config)
    except (incremental.ScanError, IndexRenderError, TransactionError) as exc:
        result = rejected_from_exception(exc)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_human(result))
    if result["status"] == "applied" and result["attention_required"]:
        return 12
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断；未继续应用。", file=sys.stderr)
        raise SystemExit(130)
