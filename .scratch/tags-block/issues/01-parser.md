# 01: 修复多行tags扫描

Status: done_local
Blocked by: none
Start head: 29786d8c1d3494729263b1d52fd80be474d0b8c6

Seam：frontmatter支持简单block标签；inline与block共用字符串解码，保证引号与Unicode转义同值哈希一致。索引应用规则复用。

原始工作树干净。成果只交本地未提交diff，不创建远端或执行真实apply。

验证：107项完整回归通过，固定快照的Spec与Standards问题均复核关闭。真实168篇只读扫描完成：163未变、5仅正文变化，exit 10为需要复核；来源和9个受控文件hash不变。证据在 outputs/知识库返工-2026-09-20/get-index-review。未commit/push/apply，未推进真实快照。
