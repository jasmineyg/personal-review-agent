# Obsidian 周期复盘 SOP

用途：在首次建立 profile baseline 后，基于本周期新增/修改文件生成本周/本月/指定时间范围复盘。

## 触发

- `/obsidian-review this-week --vault <Vault路径>`
- `/obsidian-review today --vault <Vault路径>`
- `/obsidian-review --from YYYY-MM-DD --to YYYY-MM-DD --vault <Vault路径>`

## 前置

必须已经存在：

```text
.obsidian-review-agent/vault_profile.confirmed.json
.obsidian-review-agent/review_snapshot.json
Reviews/_AgentProfile/vault_profile.draft.md
```

缺任一文件时停止周期复盘，提示用户先跑 `init-profile` 和 `confirm-profile`。

## 流程

1. `prepare` 先读取 `vault_profile.draft.md` 用户确认区，刷新结构化 profile 缓存。
2. `prepare` 已经完成隐私过滤、Vault 扫描、文件级 diff 和 block 级辅助 diff，并写出 `review_digest.latest.json`。
3. 主要读取 `review_digest.latest.json`，优先看 `executive_input`、`topic_summaries`、`file_summaries`。
4. `file_summaries` 是报告主证据：必须覆盖本周期所有新增/修改文件。
5. `changed_blocks.latest.json` 只在来源不清楚时查证文件内细节，不作为报告基点。
6. 报告写入 `Reviews/`，写成功后再运行 `finalize`，提交 pending snapshot 并更新 review state。

## 写作规则

- `vault_profile.draft.md` 用户确认区是文件夹用途、长期目标、活跃主线和复盘偏好的最高优先级上下文。
- 报告是给用户看的复盘文章，不要写成程序运行日志，也不要解释 baseline、diff、evidence package、changed blocks 等内部机制。
- 请根据用户文件夹意图，帮助用户更好地使用这个文件夹中的内容；不要套固定分类模板。
- 少样本启发：像论文笔记时可串联问题、方法差异和复习顺序；像网页剪藏或待读池时可筛选值得细读的材料并说明它们可能解决的问题；像项目记录时可提炼实验、实现、错误验证、想法和下一步。这些只是示例，遇到其他用途要自行泛化判断。
- 所有内容都要总结提炼成洞察、关系、取舍、复习线索和行动建议，不要逐文件复述已有内容。
- source_link 已经是 Obsidian 双链，原样使用；只保留少量最关键的回看入口，不要写成来源清单或原文摘抄列表。
- 新发现的文件夹用途、长期目标、活跃主线可以自动追加到 `vault_profile.draft.md` 的 Agent 候选区，但不能改用户确认区。
