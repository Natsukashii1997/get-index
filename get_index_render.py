#!/usr/bin/env python3
"""Pure rendering and validation for the five managed GET coverage-index pages."""

from __future__ import annotations

import collections
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any


HUB_FILENAME = "GET 来源索引｜总入口.md"
PENDING_FILENAME = "GET 来源索引｜待复核.md"
DOMAIN_FILENAMES = {
    "生活": "GET 来源索引｜生活.md",
    "工作/个人": "GET 来源索引｜工作个人.md",
    "工作/团队": "GET 来源索引｜工作团队.md",
}
INDEX_PAGE_FILENAMES = (
    HUB_FILENAME,
    *DOMAIN_FILENAMES.values(),
    PENDING_FILENAME,
)
TYPE_LABELS = {
    "recorder_audio": "GET 录音卡",
    "local_audio": "本地音频",
    "link": "链接",
}
FLAG_LABELS = {
    "none": "—",
    "stale": "时效",
    "hr": "人事",
    "mixed": "混合",
    "client": "客户",
    "case": "个案",
    "student": "学生",
    "private": "私人",
    "medical": "医疗",
    "duplicate": "近似重复",
    "summary_missing": "总结缺失",
}
TOPIC_ORDER = (
    "AI与自动化",
    "产品与服务",
    "项目与流程",
    "战略与经营",
    "运营与增长",
    "组织与人才",
    "调研与洞察",
    "成长与生活",
)
QUERY_URL = re.compile(r"https?://[^\s\]\[<>\"']+\?[^\s\]\[<>\"']*", re.IGNORECASE)
OBSIDIAN_TARGET_TOKENS = ("]]", "|", "#", "^", "\\")


class IndexRenderError(ValueError):
    """A validation error safe to surface through the apply command."""


def validate_source_relative_path(value: Any) -> str:
    """Return a GET source path that is safe as an Obsidian wikilink target."""

    if not isinstance(value, str) or not value:
        raise IndexRenderError("relative_path 必须是非空字符串")
    if any(token in value for token in OBSIDIAN_TARGET_TOKENS) or any(
        unicodedata.category(character) == "Cc" for character in value
    ):
        raise IndexRenderError("relative_path 包含不受支持的 Obsidian 链接字符")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise IndexRenderError("relative_path 不安全")
    normalized = candidate.as_posix()
    if not normalized.startswith("GET NOTES/Notes/") or not normalized.endswith(".md"):
        raise IndexRenderError("relative_path 不在 GET Markdown 来源树中")
    return normalized


def safe_alias(row: dict[str, Any]) -> str:
    if row["sensitivity"] == "受限":
        return f"受限来源 · {row['uid'][-8:]}"
    return row["title"].replace("|", "／").replace("[", "［").replace("]", "］")


def source_target(row: dict[str, Any]) -> str:
    return validate_source_relative_path(row["relative_path"]).removesuffix(".md")


def source_link(row: dict[str, Any]) -> str:
    return f"[[{source_target(row)}\\|{safe_alias(row)}]]"


def table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 日期 | 来源 | 类型 | 敏感性 | 稳定主题 | 核验 | 风险标记 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        topics = "、".join(row["tags"])
        lines.append(
            f"| {row['created_date']} | {source_link(row)} | {TYPE_LABELS[row['note_type']]} | "
            f"{row['sensitivity']} | {topics} | {row['review_state']} | {FLAG_LABELS[row['flag']]} |"
        )
    return lines


def domain_page(
    domain: str,
    rows: list[dict[str, Any]],
    snapshot_date: str,
) -> str:
    sensitivity = collections.Counter(row["sensitivity"] for row in rows)
    review = collections.Counter(row["review_state"] for row in rows)
    lines = [
        "---",
        "type: source-index",
        "status: active",
        "scope: GET",
        f'domain: "{domain}"',
        f"source_count: {len(rows)}",
        f"restricted_count: {sensitivity['受限']}",
        f"pending_review_count: {review['待复核']}",
        f"snapshot_date: {snapshot_date}",
        "---",
        "",
        f"# GET 来源索引｜{domain.replace('/', '')}",
        "",
        f"本页是 `{domain}` 的唯一规范来源视图，共 {len(rows)} 篇。索引只保留分类、敏感性、稳定主题和来源回链，不复制 GET 总结或转写。",
        "",
        "导航：[[系统/知识库索引/GET 来源索引｜总入口|总入口]] · [[系统/知识库索引/GET 来源索引｜待复核|待复核]]",
        "",
        f"当前敏感性：普通 {sensitivity['普通']} · 内部 {sensitivity['内部']} · 受限 {sensitivity['受限']}。"
        f"核验状态：已核验 {review['已核验']} · 规则初分 {review['规则初分']} · 待复核 {review['待复核']}。",
        "",
        "> [!warning] 权限边界",
        "> 领域不等于共享权限。“受限”来源仅本人 Obsidian 和个人 Codex 可用；阅读视图仅显示脱敏别名，点击仍可回到 GET 原笔记。",
        "",
    ]
    by_month: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_month[row["created_date"][:7]].append(row)
    for month in sorted(by_month, reverse=True):
        lines.extend([f"## {month}", ""])
        lines.extend(table(by_month[month]))
        lines.append("")
    lines.extend(
        [
            "---",
            "",
            "分类规则：[[系统/知识库治理/GET 分类与标签治理规范]]；主题词表：[[系统/知识库治理/稳定主题词表 v0.1]]。",
            "",
        ]
    )
    return "\n".join(lines)


def hub_page(rows: list[dict[str, Any]], snapshot_date: str) -> str:
    domains = collections.Counter(row["domain"] for row in rows)
    sensitivity = collections.Counter(row["sensitivity"] for row in rows)
    note_types = collections.Counter(row["note_type"] for row in rows)
    review = collections.Counter(row["review_state"] for row in rows)
    topics = collections.Counter(tag for row in rows for tag in row["tags"])
    first_date = min(row["created_date"] for row in rows)
    last_date = max(row["created_date"] for row in rows)
    return "\n".join(
        [
            "---",
            "type: source-index-hub",
            "status: active",
            "scope: GET",
            f"source_count: {len(rows)}",
            f"pending_review_count: {review['待复核']}",
            f"snapshot_date: {snapshot_date}",
            "---",
            "",
            "# GET 来源索引｜总入口",
            "",
            f"GET 已完成全量来源覆盖：{len(rows)}/{len(rows)} 篇，每篇只在一个领域页中作为规范记录出现一次。这是“找得到原件”的覆盖索引，不是重复总结，也不改变 GET 原有按日期存放的结构。",
            "",
            "## 日常入口",
            "",
            f"- [[系统/知识库索引/GET 来源索引｜生活|生活]]：{domains['生活']} 篇，只供本人入口使用。",
            f"- [[系统/知识库索引/GET 来源索引｜工作个人|工作／个人]]：{domains['工作/个人']} 篇，主要是公开学习资料和个人工作方法。",
            f"- [[系统/知识库索引/GET 来源索引｜工作团队|工作／团队]]：{domains['工作/团队']} 篇，是团队业务、项目和流程来源。",
            f"- [[系统/知识库索引/GET 来源索引｜待复核|待复核]]：{review['待复核']} 篇，仅后台治理使用。",
            "",
            "平时无需手动翻表或继续给 GET 加标签。直接向 Codex 提问；它会按问题先找项目／个人 Current、正式知识或相关知识编译，不足时看候选，需核对原话与证据时再通过本索引回到 GET 总结。",
            "",
            "## 覆盖快照",
            "",
            "| 维度 | 数量 |",
            "|---|---:|",
            f"| 总来源 | {len(rows)} |",
            f"| GET 录音卡 | {note_types['recorder_audio']} |",
            f"| 本地音频 | {note_types['local_audio']} |",
            f"| 链接笔记 | {note_types['link']} |",
            f"| 普通 | {sensitivity['普通']} |",
            f"| 内部 | {sensitivity['内部']} |",
            f"| 受限 | {sensitivity['受限']} |",
            f"| 已核验 | {review['已核验']} |",
            f"| 规则初分 | {review['规则初分']} |",
            f"| 待复核 | {review['待复核']} |",
            "",
            f"日期范围：{first_date} 至 {last_date}。主题是多选，因此下列次数之和可能大于 {len(rows)}：",
            "",
            *[f"- `{topic}`：{topics[topic]}" for topic in TOPIC_ORDER],
            "",
            "## 字段怎么看",
            "",
            "- `已核验`：为解决分类边界，只读了 GET 已有总结或链接笔记的结构化正文。",
            "- `规则初分`：frontmatter 的标题、类型、日期和原标签已足以完成初分，没有额外读转写。",
            "- `待复核`：当前依据不足；先按受限处理，只在真实问题需要时补读相关转写片段。",
            "- `风险标记`：只标识时效、人事、混合、客户、个案、学生、私人、医疗、近似重复或总结缺失，不把敏感细节复制进索引。",
            "- `受限来源 · 八位编号`：阅读视图不展示原标题，但保留可达的 Vault 内部回链。",
            "",
            "## 权限与更新边界",
            "",
            "- 本索引只是本人知识库的来源导航，不代表机械邪龙兽或团队飞书已获得访问权。",
            "- `受限` 永远只供本人入口；`内部` 也需要未来按飞书场景单独验收和授权。",
            f"- 当前覆盖快照截至 {snapshot_date}；今后新增或变化的 GET 来源只有通过增量复核与校验后才会进入本索引，不回写原来源。",
            "",
            "治理规则：[[系统/知识库治理/GET 分类与标签治理规范]]；主题词表：[[系统/知识库治理/稳定主题词表 v0.1]]；确认样板：[[系统/知识库索引/GET 来源索引｜治理样板]]。",
            "",
        ]
    )


def pending_page(rows: list[dict[str, Any]], snapshot_date: str) -> str:
    pending = [row for row in rows if row["review_state"] == "待复核"]
    lines = [
        "---",
        "type: source-index-review-queue",
        "status: active",
        "scope: GET",
        f"source_count: {len(pending)}",
        f"snapshot_date: {snapshot_date}",
        "---",
        "",
        "# GET 来源索引｜待复核",
        "",
        "这是后台治理视图，不是第四类日常目录。条目仍只在对应的生活／工作领域页中作为规范记录出现一次，本页只聚合待处理指针。",
        "",
        f"当前待复核：**{len(pending)} 篇**。",
        "",
    ]
    if pending:
        lines.extend(table(pending))
    else:
        lines.append("当前无待复核来源。")
    lines.extend(
        [
            "",
            "> [!info] 处理原则",
            "> 待复核不等于现在必须逐条重做总结。只有当真实问题命中该来源时，才读取最小必要的相关转写片段；完成判定后更新索引，不修改 GET 原文。",
            "",
            "返回：[[系统/知识库索引/GET 来源索引｜总入口|GET 来源索引总入口]]。",
            "",
        ]
    )
    return "\n".join(lines)


def render_index_pages(rows: list[dict[str, Any]]) -> dict[str, str]:
    if not rows:
        raise IndexRenderError("规范记录不能为空")
    for row in rows:
        validate_source_relative_path(row["relative_path"])
    ordered = sorted(rows, key=lambda row: (row["created_date"], row["uid"]), reverse=True)
    snapshot_date = max(row["created_date"] for row in ordered)
    pages = {HUB_FILENAME: hub_page(ordered, snapshot_date)}
    for domain, filename in DOMAIN_FILENAMES.items():
        domain_rows = [row for row in ordered if row["domain"] == domain]
        pages[filename] = domain_page(domain, domain_rows, snapshot_date)
    pages[PENDING_FILENAME] = pending_page(ordered, snapshot_date)
    return pages


def validate_index_pages(pages: dict[str, str], rows: list[dict[str, Any]]) -> None:
    if tuple(pages) != INDEX_PAGE_FILENAMES:
        raise IndexRenderError("覆盖索引页面集合不符合固定五页结构")
    combined = "\n".join(pages.values())
    if QUERY_URL.search(combined):
        raise IndexRenderError("覆盖索引包含带查询参数的 URL")
    domain_pages = {domain: pages[filename] for domain, filename in DOMAIN_FILENAMES.items()}
    pending_content = pages[PENDING_FILENAME]
    for row in rows:
        target = source_target(row)
        expected_link = source_link(row)
        expected_domain = row["domain"]
        if domain_pages[expected_domain].count(expected_link) != 1:
            raise IndexRenderError("规范记录未在对应领域页中形成唯一回链")
        if any(
            expected_link in content
            for domain, content in domain_pages.items()
            if domain != expected_domain
        ):
            raise IndexRenderError("规范记录出现在错误领域页")
        expected_pending_count = 1 if row["review_state"] == "待复核" else 0
        if pending_content.count(expected_link) != expected_pending_count:
            raise IndexRenderError("待复核聚合页回链与规范记录状态不一致")
        if row["sensitivity"] == "受限" and safe_alias(row) != (
            f"受限来源 · {row['uid'][-8:]}"
        ):
            raise IndexRenderError("受限规范记录未使用脱敏别名")
    if any(not content.endswith("\n") for content in pages.values()):
        raise IndexRenderError("覆盖索引页面必须以换行结尾")
