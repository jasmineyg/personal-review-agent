# Obsidian 首次建模 SOP

用途：首次接入一个 Obsidian Vault 时，先建立“用户校准过的环境模型”，不要直接做周期复盘。

## 触发

- `/obsidian-review init-profile --vault <Vault路径>`
- `/obsidian-review confirm-profile --vault <Vault路径>`

## 流程

1. `init-profile` 扫描 Vault，生成 `Reviews/_AgentProfile/vault_profile.draft.md`。
2. 草案必须简短、中文、面向用户校准；核心是一个“文件夹 - 作用”表。
3. 文件夹粒度要覆盖子文件夹，例如 `AI-Agent/interview`、`LLM/Paper`。
4. 文件夹作用要写成自然语言描述，不要输出“候选”这种标签词。
5. 草案可附一小段“当前进行中的主线”初步判断，列 1-4 条，每条一句话描述。
6. 不要在草案里写 Vault 概览、内容类型统计、笔记原文或长证据样本。
7. 用户直接修改表格中的“作用”和主线描述后，执行 `confirm-profile`。
8. `confirm-profile` 读取用户校准后的表格和主线描述，重新扫描当前 Vault，写入 `.obsidian-review-agent/vault_profile.confirmed.json`，并以确认时状态建立 `review_snapshot.json`。

## 硬约束

- 用户校准区优先级最高；Agent 只负责结构化，不能覆盖用户修正。
- `init-profile` 不生成周期复盘报告，不建立正式 snapshot。
- 所有 Vault 扫描默认忽略 `.obsidian-review-agent/`。
- confirmed profile 才是后续周期复盘的基础上下文。
