# Obsidian 周期复盘 SOP

用途：在已经完成 confirmed profile 后，基于本周期新增/修改文件生成本周/本月/指定时间范围复盘。

## 触发

- `/obsidian-review this-week --vault <Vault路径>`
- `/obsidian-review today --vault <Vault路径>`
- `/obsidian-review --from YYYY-MM-DD --vault <Vault路径>`

## 前置

必须已经存在：

```text
.obsidian-review-agent/vault_profile.confirmed.json
.obsidian-review-agent/review_snapshot.json
```

缺任一文件时停止周期复盘，提示用户先跑 `init-profile` 和 `confirm-profile`。

## 流程

1. `prepare` 已经完成隐私过滤、Vault 扫描、文件级 diff 和 block 级辅助 diff，并写出 `review_digest.latest.json`。
2. 主要读取 `review_digest.latest.json`，优先看 `executive_input`、`topic_summaries`、`file_summaries`。
3. `file_summaries` 是报告主证据：必须覆盖本周期所有新增/修改文件。
4. `changed_blocks.latest.json` 只在来源不清楚时查证文件内细节，不作为报告基点。
5. 报告写入 `Reviews/`，写成功后再运行 `finalize`，提交 pending snapshot 并更新 review state。

## 写作规则

- confirmed profile 是文件夹用途、长期目标、活跃主线的最高优先级上下文。
- `infer_topic_hint()` 只能当低置信新主题候选，不能覆盖 confirmed profile。
- 报告先说明本周期新增/修改了哪些文件，再按用户已确认的主题串联成逻辑线。
- 每个有修改的主题至少总结一条逻辑线：涉及哪些文件、共同说明什么进展、阻塞/下一步是什么。
- 不要把 changed blocks 当作“本周发生了什么”的唯一信号；它只用于引用具体来源。
- 不要逐条搬运 block，也不要写成 `[[来源]]: 原文片段` 清单。
- source_link 已经是 Obsidian 双链，原样使用，不要再包一层 `[[...]]`。
- 新发现的文件夹用途、长期目标、活跃主线只能写入 `vault_profile_update.latest.json` 作为建议，不自动合并。
