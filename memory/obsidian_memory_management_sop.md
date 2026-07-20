# Obsidian 复盘记忆 L0

用途：Review Agent 的记忆宪法，只规定如何写、读、合并记忆；不存用户事实。

## 核心公理

1. 无行动，不记忆：长期记忆必须来自成功工具调用、Vault 扫描、已写报告、用户确认或用户明确陈述。
2. 候选不是事实：LLM 推断只能进入 `vault_profile.draft.md` 的 Agent 候选区或 L4 pending。
3. 用户确认最高：`vault_profile.draft.md` 用户确认区 > confirmed cache > review digest 证据 > Agent 候选区。
4. 禁存易变状态：不记临时路径、PID、一次性时间戳、会话细节、短期噪声。
5. 上层只放指针：L1/L2 只保留足够定位下层的信息，细节下沉到 L3/L4。

## 层级

L1：`.obsidian-review-agent/memory/l1_context.md`
- 每轮可注入的极简索引，由 `Reviews/_AgentProfile/vault_profile.draft.md` 自动解析/刷新。
- 只放关键规则、confirmed 文件指针、活跃主线名、L3/L4 入口。
- 禁止放证据原文、完整报告、长项目纲要。

L2：confirmed facts
- `Reviews/_AgentProfile/vault_profile.draft.md` 用户确认区是人工维护入口。
- `.obsidian-review-agent/vault_profile.confirmed.json`
- `.obsidian-review-agent/memory/user_profile.confirmed.json`
- `.obsidian-review-agent/memory/mainlines_registry.json`
- 存从 profile draft 解析出的文件夹用途、长期主线、Vault 结构和主线注册表缓存。
- 主线完整内容不放 L2，只放 id、状态、来源、L3 路径。

L3：可复用 SOP 与主线档案
- `.obsidian-review-agent/memory/sops/*.md`
- `.obsidian-review-agent/memory/mainlines/<mainline_id>.md`
- SOP 存 Agent 在多次成功复盘后自动沉淀的复盘/判断/维护方法；主线档案存项目纲要、进展、漏洞、下一步。
- SOP 是 Agent 自有工作经验，不需要用户确认；用户确认只用于文件夹用途和长期主线等用户事实。

L4：历史与证据索引
- `.obsidian-review-agent/memory/profile_updates.pending.jsonl`
- `.obsidian-review-agent/memory/profile_updates.history.jsonl`
- `.obsidian-review-agent/memory/review_history.jsonl`
- 记录候选、apply/reject、复盘运行、证据路径；尽量 append-only。

## 写入流程

1. propose：复盘后把新主线、文件夹用途、主线进展/漏洞追加到 Agent 候选区或 L4 pending；SOP 经验由 finalize 自动写入 sop_registry/sops。
2. user-edit：用户只需在 Obsidian 修改/删除/移动 `vault_profile.draft.md` 内容。
3. sync：下次运行自动解析用户确认区，刷新 L1/L2 缓存；Agent 候选区仍只是弱信号。
4. reject：用户删除或改写候选即视为否定/修正；必要时写 history，避免重复提出。
5. compact：历史过长时压缩文字，但保留来源、确认状态和证据指针。

## 分类规则

- 记忆制度、写入红线 -> L0
- 每轮都需要知道的入口/短规则 -> L1
- 稳定且已确认的用户/Vault/主线元数据 -> L2
- 可复用方法或单条主线长期状态 -> L3
- 原始证据、候选、操作历史、复盘索引 -> L4
- 其他内容不存

## 隐私红线

- 必须遵守 `config.json` 的 `ignore_dirs` 与 `ignore_tags`。
- 被隐私过滤的内容不得进入 digest、profile、mainline、history。
- 禁止写入密钥、token、密码、cookie、私钥、原始会话数据。

## 冲突处理

发现冲突时不要覆盖 confirmed；写入 `profile_conflict` 候选并请求用户确认。
