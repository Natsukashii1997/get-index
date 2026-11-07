# 当前状态

更新：2026-11-07。GET 索引增量维护工具已转为活跃项目（数字分身名册 v1.3），建了 GitHub 私有仓库，只放代码。唯一下一步：常浩下次跑「运行 GET 索引维护.command」时，确认多行 tags 兼容修复在真实 GET NOTES 上扫描通过。

## 转为活跃：补协作骨架、去掉索引数据后建私有远端（2026-11-07，Claude，常浩说「get-index 调为活跃、建 GitHub 仓库」，选「只推代码」；已提交并推送、无需发布）

- 时间：开工 `2026-11-07 19:52`（`date '+%F %H:%M'` 记录）。来由：常浩看过全量项目盘点后，把 get-index 从「只登记」调为活跃。
- 改了哪些文件：
  - 9-20～9-28 留下没提交的改动一并提交：多行 tags 兼容（`get_index_incremental.py`、`tests/test_block_tags.py`、`.scratch/tags-block/`）、操作入口小改（`get_index_operator.py`、`get_index_render.py`、`tests/test_operator.py`）、`.scratch/get-incremental/issues/04` 补记。
  - `part-*.jsonl` 三份和 `state/accepted-snapshot.json` 移出跟踪（文件留在本机原位），加进 `.gitignore`。
  - `AGENTS.md`：「不连接远程仓库」改为「远端只放代码，数据不入库不上传」；issue 看板改为 GitHub；末尾加「协作骨架」一节。`docs/agents/issue-tracker.md` 换成 GitHub 版。
  - 用「项目起步」补铺：`CURRENT.md`（本文件）、`docs/LESSONS.md`、`docs/OPERATIONS.md`、`docs/templates/handoff.md`、`tests/handoff-format.test.mjs`、`scripts/check-rules.mjs`（保持默认，Python 项目里没有它扫的目录，自动跳过）与 `tests/check-rules.test.mjs`、`.github/pull_request_template.md`。
  - 历史重建：旧历史里带着索引数据（177 条笔记的标题与分类，部分标受限），所以远端用一份新的单提交历史；旧历史连同数据打包在 `~/Archive/repos/备份/get-index-旧历史含数据-20261107.tgz`。
- 测试：`python3 -m unittest discover -s tests` 原样结果 `Ran 108 tests` / `OK`（提交前的旧 HEAD 为 103 项 OK）；`node --test tests/*.test.mjs` 原样结果 `ℹ pass 4` / `ℹ fail 0`；`node scripts/check-rules.mjs` 输出 `通用规则检查通过`。
- 验证：推送后用 `gh api` 列远端文件，确认没有 `part-*.jsonl` 和 `state/`。
- 截图：无。
- 发布与回读：无需发布。
- 与授权不一致之处、遗留问题、范围外发现：多行 tags 修复还没在真实 GET NOTES 上跑过「仅检查」；`.scratch/` 旧本地票没迁到 GitHub Issues。
- 本轮新坑：无。
