# Obsidian 首次建模 SOP

用途：首次接入一个 Obsidian Vault 时，先建立“用户校准过的环境模型”，不要直接做周期复盘。

## 触发

- `/obsidian-review init-profile --vault <Vault路径>`
- `/obsidian-review confirm-profile --vault <Vault路径>`（首次建立 baseline 时使用）

## 流程

1. `init-profile` 扫描 Vault，生成 `Reviews/_AgentProfile/vault_profile.draft.md`。
2. 这个 Markdown 是用户唯一需要长期维护的 profile 入口，分为“用户确认区”和“Agent 候选区”。
3. 文件夹粒度要覆盖子文件夹，例如 `AI-Agent/interview`、`LLM/Paper`。
4. 用户确认区包含文件夹用途、长期主线、复盘偏好；用户可直接改、删、补充。
5. Agent 候选区只放自动发现的新文件夹/新主线/修改建议，用户可按需检查。
6. 不要在草案里写 Vault 概览、内容类型统计、笔记原文或长证据样本。
7. 首次运行时，用户直接修改用户确认区后执行 `confirm-profile`，用于建立初始 snapshot。
8. 后续复盘不要求再次 confirm；`prepare` 会自动读取 profile draft，刷新 `.obsidian-review-agent/vault_profile.confirmed.json` 缓存。

## 硬约束

- 用户确认区优先级最高；Agent 只能自动追加候选区，不能覆盖用户修正。
- `init-profile` 不生成周期复盘报告，不建立正式 snapshot。
- 所有 Vault 扫描默认忽略 `.obsidian-review-agent/`。
- profile draft 的用户确认区是后续周期复盘的基础上下文；confirmed profile JSON 只是结构化缓存。
