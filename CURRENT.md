# 当前状态

当前阶段目标：常态维护（不设阶段目标）；多行 tags 修复待真实 GET NOTES 扫描验证。（2026-11-08 常浩确认，digital-twin#114）

更新：2026-11-07。GET 索引增量维护工具已转为活跃项目（数字分身名册 v1.3），建了 GitHub 私有仓库，只放代码。唯一下一步：常浩下次跑「运行 GET 索引维护.command」时，确认多行 tags 兼容修复在真实 GET NOTES 上扫描通过。

## 装提交、推送前的必经检查钩子（2026-11-20，Claude，常浩批复盘 P4、P6「批（按改过的）」；已提交、PR 待合并，无需发布）

- 时间：开工 `2026-11-20 11:02`。★ 来由：跨项目复盘 P6「检查脚本接进必经环节」：没有 CI 的仓库装 pre-push 钩子；P4 的 commit-msg 钩子一起装。钩子来自起步骨架 0.4.0（my-skills#112）。
- 改了哪些文件：`scripts/hooks/pre-push`（推送前跑 `node scripts/check-rules.mjs` 和 `node --test tests/*.test.mjs && python3 -B -m unittest discover -s tests`）、`scripts/hooks/commit-msg` + `commit-msg.mjs`（修复类提交正文要有「同类：」一行）、`tests/hooks.test.mjs`（单测 + 临时仓库真跑）、`AGENTS.md`（加安装命令 `git config core.hooksPath scripts/hooks`）；本文件。
- 测试：`node --test tests/hooks.test.mjs` 原样 `ℹ pass 3` / `ℹ fail 0`；在本 worktree 里 `sh scripts/hooks/pre-push` 退出码 0（证据 `~/session-retro/2026-10-18_2026-11-18/evidence/p6-hooks/get-index.txt`）。
- 验证环境：替身（临时 git 仓库、本机裸仓库）；本仓库的 `core.hooksPath` 合并后才在本机装，装后真推一次再算验收。
- 外部前提与实测：依赖 git 的 `core.hooksPath` 和钩子退出码，已在临时仓库实测。
- 与授权不一致之处、遗留问题、范围外发现：无。
- 本轮新坑：无。

## LESSONS 升级行补层级词、加规则检查（2026-11-19，Claude，跨项目复盘收尾子 agent，常浩批了「老项目接入 LESSONS 升级行检查：一次补齐」；已提交到 `claude/lessons-level`、PR 待合并、无需发布）

- 时间：开工 `2026-11-19 10:18`（`date '+%F %H:%M'` 记录）。来由：跨项目复盘 D 批，各项目 LESSONS 的「升级」行统一带层级词，并由规则检查守住。
- 改了哪些文件：`scripts/check-rules.mjs`（加 `lessons` 规则：每条 `## L-xx` 要有以〔结构〕〔机检〕〔体检〕〔文字〕〔提醒〕开头的「- 升级：」一行，写法照起步骨架）；`tests/check-rules.test.mjs`（加 1 条测试：缺升级行、升级行不带层级词各报一次，格式样例和合规条目不误报）；`docs/LESSONS.md`（「怎么升级」补一条层级词要求，格式样例改成「〔提醒〕未升级。」，与起步骨架一致）；本仓库 LESSONS 还没有真实条目，规则在真实文件上为绿。另修起步骨架带来的漏报：判断占位标题前先去掉反引号里的内容（标题里的 `<all_urls>` 之类原来会让整条不查），写法同 chatgpt-clipper#10（`cd80285`），并加 1 条测试；骨架本身由主会话另行处理。本节交接。
- 测试：`python3 -m unittest discover -s tests` 原样结果改前、改后都是 `Ran 108 tests` / `OK`；`node --test tests/*.test.mjs` 改前 `ℹ pass 4` / `ℹ fail 0`，改后 `ℹ pass 6` / `ℹ fail 0`；`node scripts/check-rules.mjs` 改前改后都输出 `通用规则检查通过`。
- 验证：在 worktree `~/Sandbox/worktrees/get-index-lessons-level` 里跑；改前改后日志在 `~/Library/Application Support/get-index/evidence/2026-11-19-lessons-level/`。
- 截图：无。
- 发布与回读：无需发布。
- 与授权不一致之处、遗留问题、范围外发现：规则检查没有接进 CI 或提交前钩子，只在手动跑测试时生效（照实记录，没为此改结构）。
- 本轮新坑：无。

## 交叉审查返修 F-08 / F-07（2026-11-12，Codex，常浩授权核实报告并返修；未提交、未发布）

- 时间：开工 `2026-11-12 15:46`（`date '+%F %H:%M'` 记录）。分支 `codex/audit-fix-1112`，基线 `2af2e6995a613ba475956d1b7fbd0ad2b0735b67`，开工工作区干净；核实指定 `xreview/report.md` 的两项高危遗漏，均成立。
- 改了哪些文件：`get_index_incremental.py` 强制 JSON 写入携带完整目录身份，共用绑定与复核；`get_index_operator.py` 给复核包创建的四个 JSON 文件和两条人工保存路径绑定 Vault 外校验与目录身份，复用已有包时同样保护，并将同入口建目录改为经已验证目录 FD 创建、chmod 改为 fchmod；`.gitignore` 全局排除 `apply-transaction.json` 与 `apply-transaction-*/`；`tests/test_operator.py` 新增三项竞争回归（24 个 JSON 场景、4 个建目录场景）；`tests/test_atomic_apply.py` 复用原残留断言并新增非 state 快照目录中断回归；`tests/test_review_drafts.py` 两处既有物理别名测试补必需参数，原断言不变；本文件补交接。无顺手修改，无正常用户可见行为、格式或已定内容变化。
- 测试：F-08 两项 JSON 测试修前 `Ran 2 tests in 0.216s` / `FAILED (failures=18)`，修后 `Ran 2 tests in 0.281s` / `OK`；同入口建目录修前 `Ran 1 test in 0.037s` / `FAILED (failures=2)`，修后连同原 F-08 回归 `Ran 4 tests in 0.618s` / `OK`。F-07 非默认目录修前 `Ran 1 test in 0.030s` / `FAILED (failures=1)`，修后连同默认目录 `Ran 2 tests in 0.061s` / `OK`。最终 `/usr/bin/python3 -B -m unittest discover -s tests`：`Ran 114 tests in 12.694s` / `FAILED (failures=8, errors=11)`；既有 Python 3.9 不支持 `zip(..., strict=True)`，未计作通过；在指定证据目录提取基线代码、测试与命令入口重跑：`Ran 110 tests in 12.478s` / `FAILED (failures=8, errors=11)`，失败/错误的 19 个测试名逐项一致。现有 Python 3.14.5 的 `python3 -B -m unittest discover -s tests`：`Ran 114 tests in 12.822s` / `OK`。`node --test tests/*.test.mjs`：`ℹ tests 4` / `ℹ pass 4` / `ℹ fail 0`；完整输出见 `rework/node-full.log`。
- 验证：仅使用指定持久证据目录下的合成夹具；24 个 JSON 场景验证 GET 来源字节、mtime、目录清单不变，且没有在 GET 目录创建过临时文件；4 个建目录场景验证 GET 来源目录清单与文件不变。未读真实 GET、索引数据、运行状态或私人日志，未联网、未监听端口、未起浏览器。首次全量暴露既有测试遗漏必需参数，已补齐并重跑，失败日志保留；完整命令与原样结果追加于指定 `summary.md` 的「交叉审查返修」。
- 截图：无，未改界面；浏览器、监听端口类验证未做，不声称通过。
- 发布与回读：未提交、未推送、未合并、未发布；HEAD 保持基线，草稿 PR #1 尚未包含本轮未提交返修，正式使用环境未采用。
- 与授权不一致之处、遗留问题、范围外发现：无越界修改，无需要常浩确认的行为变化。报告未列中危；仓库没有版本号或 CHANGELOG 及对应更新约定，不新增。建议另开 issue 处理既有 Python 3.9 兼容性/最低版本声明；未在本轮修复。多行 tags 真实 GET 扫描继续待验，保留原常态维护状态。
- 本轮新坑：无（属于原审计同类入口遗漏的返修，未新增长期规则）。

## 修复独立审计 F-08 / F-07（2026-11-12，Codex，常浩授权仅修两项并测试；未提交、未发布）

- 时间：开工 `2026-11-12 10:55`（`date '+%F %H:%M'` 记录）。在 `codex/audit-fix-1112` 隔离 worktree 修复独立审计确认的两项高严重度问题；起点 `de6604d1f75bf7556e9837a09d29c40a8c732b2d`，开工工作区干净。
- 改了哪些文件：`get_index_incremental.py` 在草稿路径校验时保存父目录与祖先目录身份，写入前及替换前复核，并校验实际打开的父目录 FD 身份；`.gitignore` 忽略完整 `state/` 和 apply / journal / recovery 三类事务临时文件；`tests/test_review_drafts.py` 新增祖先目录竞争回归（两个时点 × 两种重定向）；`tests/test_atomic_apply.py` 新增真实合成事务中断残留的 Git 忽略回归；本文件补交接。无顺手修改、无业务行为变化。
- 测试：两项均先补测试并在修前确认断言失败，再修复通过；F-08 `Ran 1 test in 0.508s` / `FAILED (failures=3)` → `Ran 1 test in 0.403s` / `OK`；F-07 `Ran 1 test in 0.053s` / `FAILED (failures=1)` → `Ran 1 test in 0.102s` / `OK`。优先 `/usr/bin/python3 -B -m unittest discover -s tests`：`Ran 110 tests in 18.325s` / `FAILED (failures=8, errors=11)`，系统 Python 3.9.6 不支持既有 `zip(..., strict=True)`；本机 `python3 -B -m unittest discover -s tests`（Python 3.14.5）：`Ran 110 tests in 39.005s` / `OK`；`node --test tests/*.test.mjs`：`ℹ tests 4` / `ℹ pass 4` / `ℹ fail 0`。
- 验证：全部使用指定证据目录下自动清理的合成夹具；未访问真实 GET、`data/`、运行状态或私人日志，未联网、未提交、未推送。`git diff --check`、`zsh -n '运行 GET 索引维护.command'` 退出 0；`node scripts/check-rules.mjs` 输出 `通用规则检查通过`。总结和原样日志：`/Users/nat/Library/Application Support/get-index/evidence/2026-11-12-codex-fix/summary.md`。
- 截图：无，未改界面。
- 发布与回读：未发布；仅本 worktree 的未提交候选修复，目标使用环境尚未采用。
- 与授权不一致之处、遗留问题、范围外发现：无越界改动。系统 Python 3.9 兼容性未修改；F-01～F-06 不在本轮修复范围。原有多行 tags 真实扫描待验状态保持不变。项目引用的旧 Vault 工作卡路径已不存在，本轮只依据当前明确授权与本地约定修复，不据此推断业务状态或改写已定内容。
- 本轮新坑：无（本轮修复来自既有审计）。

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
