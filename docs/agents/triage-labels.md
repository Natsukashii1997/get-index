# Triage Labels

| Canonical role | Local label | Meaning |
|---|---|---|
| `needs-triage` | `needs-triage` | 需要维护者判断是否进入项目 |
| `needs-info` | `needs-info` | 缺少会改变实现路线的信息 |
| `ready-for-agent` | `ready-for-agent` | 范围、依赖和验收已清楚，可开始执行 |
| `ready-for-human` | `ready-for-human` | 需要常浩完成无法由 AI 代替的步骤 |
| `wontfix` | `wontfix` | 已决定不执行并保留原因 |

Ticket 的 `Status:` 使用以上精确字符串；执行开始、受阻、完成等进度同时写入 Ticket 正文和项目工作卡，不用标签冒充交付证据。
