# 当前状态

当前阶段目标：常态维护（不设阶段目标）；多行 tags 修复待真实 GET NOTES 扫描验证。（2026-11-08 常浩确认，digital-twin#114）

更新：2026-11-07。GET 索引增量维护工具已转为活跃项目（数字分身名册 v1.3），建了 GitHub 私有仓库，只放代码。唯一下一步：常浩下次跑「运行 GET 索引维护.command」时，确认多行 tags 兼容修复在真实 GET NOTES 上扫描通过。

## LESSONS 升级行补层级词、加规则检查（2026-11-19，Claude，跨项目复盘收尾子 agent，常浩批了「老项目接入 LESSONS 升级行检查：一次补齐」；已提交到 `claude/lessons-level`、PR 待合并、无需发布）

- 时间：开工 `2026-11-19 10:18`（`date '+%F %H:%M'` 记录）。来由：跨项目复盘 D 批，各项目 LESSONS 的「升级」行统一带层级词，并由规则检查守住。
- 改了哪些文件：`scripts/check-rules.mjs`（加 `lessons` 规则：每条 `## L-xx` 要有以〔结构〕〔机检〕〔体检〕〔文字〕〔提醒〕开头的「- 升级：」一行，写法照起步骨架）；`tests/check-rules.test.mjs`（加 1 条测试：缺升级行、升级行不带层级词各报一次，格式样例和合规条目不误报）；`docs/LESSONS.md`（「怎么升级」补一条层级词要求，格式样例改成「〔提醒〕未升级。」，与起步骨架一致）；本仓库 LESSONS 还没有真实条目，规则在真实文件上为绿。本节交接。
- 测试：`python3 -m unittest discover -s tests` 原样结果改前、改后都是 `Ran 108 tests` / `OK`；`node --test tests/*.test.mjs` 改前 `ℹ pass 4` / `ℹ fail 0`，改后 `ℹ pass 5` / `ℹ fail 0`；`node scripts/check-rules.mjs` 改前改后都输出 `通用规则检查通过`。
- 验证：在 worktree `~/Sandbox/worktrees/get-index-lessons-level` 里跑；改前改后日志在 `~/Library/Application Support/get-index/evidence/2026-11-19-lessons-level/`。
- 截图：无。
- 发布与回读：无需发布。
- 与授权不一致之处、遗留问题、范围外发现：规则检查没有接进 CI 或提交前钩子，只在手动跑测试时生效（照实记录，没为此改结构）。
- 本轮新坑：无。

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
