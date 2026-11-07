# 02: 为新增与关键变化生成保守复核记录

**What to build:** 当 Ticket 01 检测到新增或会影响索引的变化时，只从 frontmatter 带入可确认事实，生成少量待复核记录；无法可靠确定的治理字段保持待复核并从严处理，不重新推断已有 168 篇来源。

**Blocked by:** 01: 只读识别 GET 增量变化.

**Status:** complete

**Execution:** complete

- [x] 新增来源能生成一条结构合法的复核草稿，并保留来源相对路径、UID、标题、日期和来源类型等可核验事实。
- [x] 未经确认的 `sensitivity` 默认 `受限`，`review_state` 为 `待复核`；不为消灭待复核而读取音频或完整转写。
- [x] 仅内容变化沿用原规范记录并进入变化报告，不自动重分类领域、敏感性或稳定主题。
- [x] 草稿缺少必需字段、出现不受控枚举或检测基线失效时退出失败，不修改已接受记录。
- [x] 自动测试覆盖新增、关键元数据变化、仅内容变化和非法草稿，输出不包含来源正文。

## Implementation record

- Start commit: `63e49d3585ab7c4e3c99c489a232fbbc93d5ee6d`
- Completion commit: `df8b0ab49acc1a26bb54a0cd98b70d3818412c37`
- Validation: `python3 -m unittest discover -s tests -v` 40/40 PASS；`py_compile` 与 `git diff --check` PASS；全量规范记录 168/168 PASS；真实 Vault 扫描为 168 篇 `unchanged`、其他变化均为 0；真实零变化复核包为 0 草稿、0 通知、0 未决字段且权限 `0600`；GET Markdown 前后组合 SHA-256 均为 `90eadd8df9e66de18b467c64939bc52423a468b368e9e2c23f2cedb47017be78`。未写 GET、索引页或增量快照，临时验证文件已清理。
- Standards review: 固定范围 `63e49d3…df8b0ab` PASS，阻塞 0；URL 大小写、查询参数泄漏、全局路径／UID 唯一、父目录 TOCTOU 与项目术语均已闭环。保留“后续拆分过长单文件”的非阻塞重构建议。
- Spec review: 固定范围 `63e49d3…df8b0ab` PASS，阻塞 0、scope creep 0；确认 `valid_review_draft` 只表示结构合法，不等同人工复核完成或可应用。

## Comments

- 2026-09-15：常浩确认四张 Ticket 的粒度与线性依赖。
- 2026-09-15：所有待持久化来源标题先移除 URL 查询参数，URL scheme 大小写不敏感；原始 GET 标签只保留摘要，正文不进入草稿。
- 2026-09-15：草稿只允许写到 Vault 外的单硬链接普通文件；最终替换前再次核对 GET 来源指纹，失败不替换已有输出。
