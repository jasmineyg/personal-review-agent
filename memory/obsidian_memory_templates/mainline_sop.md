# <主线名称> SOP

用途：这是一条长期主线的 L3 SOP，记录未来复盘这条主线时应如何判断、归纳和更新。

## 触发

- 周复盘涉及 `<主线名称>` 的文件、目录、标签或用户明确提到该主线。

## 读取顺序

1. 先读 L1，确认主线 id 与档案路径。
2. 读 `mainlines/<mainline_id>.md`，了解当前目标、阻塞、下一步。
3. 读 `review_digest.latest.json` 中相关文件摘要。
4. 证据不清时才读 `changed_blocks.latest.json`。

## 更新规则

- 已验证进展 -> 写 pending `mainline_progress`。
- 新发现漏洞 -> 写 pending `mainline_gap`。
- 下一步建议 -> 写 pending `mainline_next_step`。
- 主线目标变化 -> 写 pending，必须用户确认后才能 apply。

## 输出要求

- 先说明本周期这条主线的真实推进。
- 再说明阻塞、漏洞或长期风险。
- 最后给 1-3 个下一步候选。
- 不搬运原文，不写 block 清单，只保留证据链接。
