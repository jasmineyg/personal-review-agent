# Obsidian Review SOP Index

这个文件只做入口分流，避免把首次建模和周期复盘混在同一份长 SOP 中。

## 选择 SOP

| 场景 | 读取 |
|---|---|
| 首次进入 Vault、生成/确认环境模型 | `memory/obsidian_profile_init_sop.md` |
| 本周/本月/指定时间范围复盘 | `memory/obsidian_periodic_review_sop.md` |

## 命令

```text
/obsidian-review init-profile --vault <Vault路径>
/obsidian-review confirm-profile --vault <Vault路径>
/obsidian-review this-week --vault <Vault路径>
/obsidian-review --from YYYY-MM-DD --to YYYY-MM-DD --vault <Vault路径>
```

## 总原则

- 首次运行只建立环境模型草案，不直接周期复盘。
- `vault_profile.draft.md` 的用户确认区是后续复盘的最高优先级上下文；confirmed profile JSON 是自动缓存。
- 周期复盘只读周期 SOP，避免模型在长文中混淆阶段。
