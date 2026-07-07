# L4 JSONL 记录模板

L4 是历史与证据层。每个非空行必须是一个 JSON 对象。

## profile_updates.pending.jsonl

候选更新。不是 confirmed facts。

```json
{
  "id": "upd_YYYYMMDD_001",
  "schema_version": 1,
  "status": "pending",
  "kind": "mainline_progress",
  "target_layer": "L3",
  "target_id": "example_mainline",
  "proposal": "本周期推进了某条主线的一个可验证进展。",
  "confidence": "agent_candidate",
  "evidence": [
    {
      "type": "review_report",
      "path": "Reviews/YYYY-MM-DD_this-week_周期复盘.md"
    }
  ],
  "created_at": "YYYY-MM-DDTHH:MM:SS+08:00"
}
```

常用 `kind`：

- `mainline_progress`
- `mainline_gap`
- `mainline_next_step`
- `new_mainline_candidate`
- `folder_role_candidate`
- `user_preference_candidate`
- `workflow_preference_candidate`
- `profile_conflict`

## profile_updates.history.jsonl

记录 apply/reject/supersede/compact。

```json
{
  "id": "hist_YYYYMMDD_001",
  "schema_version": 1,
  "operation": "apply",
  "source_update_id": "upd_YYYYMMDD_001",
  "target_layer": "L3",
  "target_id": "example_mainline",
  "summary": "将本周期进展写入示例主线档案。",
  "applied_paths": [
    ".obsidian-review-agent/memory/mainlines/example_mainline.md"
  ],
  "decided_by": "user",
  "decided_at": "YYYY-MM-DDTHH:MM:SS+08:00"
}
```

## review_history.jsonl

记录每次完成的复盘运行。

```json
{
  "id": "review_YYYYMMDD_001",
  "schema_version": 1,
  "period": "this-week",
  "date_start": "YYYY-MM-DD",
  "date_end": "YYYY-MM-DD",
  "report_path": "Reviews/YYYY-MM-DD_this-week_周期复盘.md",
  "digest_path": ".obsidian-review-agent/review_digest.latest.json",
  "changed_files": 0,
  "changed_blocks": 0,
  "mainlines_touched": [
    "example_mainline"
  ],
  "created_pending_updates": [
    "upd_YYYYMMDD_001"
  ],
  "finalized_at": "YYYY-MM-DDTHH:MM:SS+08:00"
}
```
