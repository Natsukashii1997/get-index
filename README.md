# GET 索引增量维护

这是 Obsidian 人生知识库 `KB-04` 的本地维护工具。日常只使用仓库根目录的：

`运行 GET 索引维护.command`

面向常浩的权威操作说明位于：

`/Users/nat/Documents/Obsidian Vault/系统/GET 索引维护使用说明.md`

## 快速边界

- `GET NOTES/**` 永远只读。
- 默认流程完全在本机确定性执行，不调用模型、不联网，也不创建定时任务。
- `--check-only` 严格零写；普通入口只有在人工复核完整并精确输入“应用”后，才可能更新九个受控目标。
- 运行文件保存在仓库的 `.runtime/`，仅包含治理所需元数据，不复制 GET 正文、总结或转写。
- 中断或拒绝后重新运行同一入口；不要手改或删除 draft、reviewed、journal、snapshot 和 JSONL 文件。

## 维护者验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile get_index_incremental.py get_index_operator.py get_index_render.py get_index_transaction.py validate_get_index.py render_get_index_patch.py
zsh -n '运行 GET 索引维护.command'
git diff --check
```

真实 Vault 验收只允许运行：

```bash
./运行\ GET\ 索引维护.command --check-only --format json
```

不得为了测试真实应用而在 `GET NOTES/**` 制造来源；新增、异常与中断链路只在临时夹具中验证。
