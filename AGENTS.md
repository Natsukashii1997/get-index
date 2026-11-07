# GET 来源索引技术项目

本目录承载 Obsidian 人生知识库项目中 `KB-04 GET 增量维护闭环` 的技术实现与验证。业务目标、有效需求、权限边界和项目状态以 `/Users/nat/Documents/Obsidian Vault/系统/知识库项目/Obsidian 人生知识库｜项目工作卡.md` 为准。

## 项目边界

- `/Users/nat/Documents/Obsidian Vault/GET NOTES/**` 是严格只读来源层；任何实现都不得在其中新建、编辑、移动、重命名或删除文件。
- 默认先检测、验证和生成候选输出；只有 Ticket 明确授权且全部前置检查通过时，才可更新来源层之外的 GET 索引页。
- 异常、缺失分类、重复 UID、无法解析的 frontmatter 或不完整输入必须失败关闭；不得把未扫描输入解释为“无新增”。
- 不读取音频，不批量读取完整转写，不把敏感原标题或内容写入日志和测试夹具。
- 远端（GitHub 私有仓库 `Natsukashii1997/get-index`，2026-11-07 建）只放代码、测试和文档；GET 元数据、索引数据（`part-*.jsonl`、`state/accepted-snapshot.json`、`.runtime/`）和 Vault 内容一律不入库、不上传，`.gitignore` 已排除。

## Agent skills

### Issue tracker

需求与任务用本仓库 GitHub Issues（2026-11-07 起）；`.scratch/` 里是之前的本地票，只作历史。见 `docs/agents/issue-tracker.md`。

### Triage labels

The local tracker uses the standard five-role label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context project. See `docs/agents/domain.md` for how to consume domain documentation.

## 协作骨架

2026-11-07 用「项目起步」补铺：交接写在 [CURRENT.md](CURRENT.md) 顶部，骨架照 [docs/templates/handoff.md](docs/templates/handoff.md)；坑记在 [docs/LESSONS.md](docs/LESSONS.md)；交接写法与验收见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。改动怎么走见 `/Users/nat/Documents/ChatGPT/个人/work/agent-kit/工程流程.md`。

测试：`python3 -m unittest discover -s tests`（业务）；`node --test tests/*.test.mjs`（交接格式与通用规则）。
