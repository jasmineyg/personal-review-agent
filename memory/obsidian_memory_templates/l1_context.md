# Review Agent L1

L0: memory/obsidian_memory_management_sop.md
ProfileDraft: Reviews/_AgentProfile/vault_profile.draft.md
VaultProfile: .obsidian-review-agent/vault_profile.confirmed.json
UserProfile: .obsidian-review-agent/memory/user_profile.confirmed.json
Mainlines: .obsidian-review-agent/memory/mainlines_registry.json
SOPs: .obsidian-review-agent/memory/sops/*.md
Pending: .obsidian-review-agent/memory/profile_updates.pending.jsonl
History: .obsidian-review-agent/memory/profile_updates.history.jsonl

RULES:
- ProfileDraft 用户确认区才是最高优先级事实；Agent 候选区只是弱信号。
- 新主线/文件夹用途/用户偏好/长期目标只能追加到 Agent 候选区，不能自动改用户确认区。
- 周复盘先读 review_digest.latest.json；证据不清时再查 changed_blocks.latest.json。
- 写长期记忆前必须读 L0；复盘前自动解析 ProfileDraft 刷新本文件。

活跃主线:
- <mainline_id>: <主线名> -> .obsidian-review-agent/memory/mainlines/<mainline_id>.md

复盘偏好:
- 语言: zh-CN
- 风格: 结论先行，按主线串联，不列 block 清单
