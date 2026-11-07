# 01: 只读识别 GET 增量变化

**What to build:** 提供一个端到端的只读检测入口，把当前 GET Markdown 与已接受快照比较，并用非技术人员能理解的结果区分新增、索引元数据变化、仅内容变化、缺失和无变化；检测过程不写 Vault，也不推进任何已接受状态。

**Blocked by:** None (can start immediately).

**Status:** complete

**Execution:** complete

- [x] 无变化时明确报告 168 篇 `unchanged`、其他变化为 0，退出成功且 Vault 内容零变化。
- [x] 能在临时夹具中分别识别 `added`、`index_metadata_changed`、`content_only_changed` 和 `missing`。
- [x] 重复 UID、损坏 frontmatter 或不安全来源路径必须退出失败，不生成“无新增”结论。
- [x] 报告只包含检测所需的路径、UID、类型和计数，不复制总结、转写或正文。
- [x] 自动测试从公开命令入口观察退出码和报告，真实 GET 只执行只读检查。

## Implementation record

- Start commit: `0448740e30724542fee4058a6e842ba8f5dc83e0`
- Completion commit: `4e7c234552d43b617c0c3fcca0768b2316573da1`
- Validation: `python3 -m unittest -v tests/test_incremental_scan.py` 16/16 PASS；`py_compile` PASS；现有全量索引校验 168/168 PASS；真实 Vault 报告 168 篇 `unchanged`、其余四类为 0；GET Markdown 前后组合 SHA-256 均为 `8d2156853d3286ef4f831bfe3fc2b17e6d801ad19ff1bd7fcda6fe15816812e6`。
- Standards review: 最终固定范围 `0448740…4e7c234` PASS，阻塞 0；保留两个不影响本 Ticket 的未来重构判断项（记录类型、测试夹具去重），不扩大本次范围。
- Spec review: 最终固定范围 `0448740…4e7c234` PASS，阻塞 0、建议 0；未混入 Ticket 02–04 职责。

## Comments

- 2026-09-15：常浩确认四张 Ticket 的粒度与线性依赖。
- 2026-09-15：实现口径固定为：来源 `tags` 虽不同于 8 个受控稳定主题，但属于索引判断证据；只保存规范化后的 SHA-256，标签变化归入 `index_metadata_changed` 并进入复核，报告不暴露标签值，也不自动重分类。
- 2026-09-15：168 条初始快照仅作一次性迁移。迁移前逐条核对旧快照的路径、UID、标题、日期、类型和全文哈希与当前来源完全一致，并先通过现有 168/168 全量索引校验；公开 `scan` 命令不含初始化、接受、写索引或推进快照能力。
- 2026-09-15：候选提交 `05f5060` 完成首次双轴审查；依据 Standards 反馈统一用户文案为“GET 来源／增量快照／待复核”，补充“未推进增量快照”可观察结果，修复提交为 `4e7c234`，复验与最终双轴审查通过。
