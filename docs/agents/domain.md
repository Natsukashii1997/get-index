# Domain Docs

## Before exploring

- Read `CONTEXT.md` when it exists.
- Read applicable decisions in `docs/adr/` when they exist.
- Read the linked Obsidian project work card for current business requirements, permissions and acceptance status.

Missing domain files are not blockers. Create or revise them only when a stable term or hard-to-reverse technical decision genuinely needs a durable record.

## Layout

This is a single-context technical project:

```text
/
├── AGENTS.md
├── CONTEXT.md              # created when stable domain language needs it
├── docs/
│   ├── agents/
│   └── adr/
├── .scratch/
└── source and test files
```

## Vocabulary and conflicts

Use the project terms exactly: `GET 来源`, `规范记录`, `覆盖索引`, `增量快照`, `待复核`, `domain`, `sensitivity`, and `稳定主题`. Do not use “同步成功” to mean that a source is classified, indexed, retrievable or safe to share.

If a proposed change conflicts with the Obsidian work card, Vault `AGENTS.md` or an ADR, surface the conflict and stop the affected work until the authoritative requirement is resolved.
