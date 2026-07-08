"""Obsidian review command router for GenericAgent.

This module turns a natural-language Obsidian review request, or an explicit
`/obsidian-review` command, into an in-session GenericAgent task.  The task is
still executed by GenericAgent itself: it runs the deterministic helper,
reads `review_digest.latest.json` as the main writing input, writes the
Markdown report back into the vault, and finalizes the snapshot.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Optional


CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_REL = "memory/obsidian_review/obsidian_review.py"
SOP_REL = "memory/obsidian_periodic_review_sop.md"
SCRIPT_PATH = os.path.join(CODE_ROOT, "memory", "obsidian_review", "obsidian_review.py")
SOP_PATH = os.path.join(CODE_ROOT, "memory", "obsidian_periodic_review_sop.md")

PROFILE_COMMANDS = {"init-profile", "confirm-profile"}
PERIOD_ALIASES = {
    "today": "today",
    "今天": "today",
    "今日": "today",
    "this-week": "this-week",
    "this week": "this-week",
    "本周": "this-week",
    "这周": "this-week",
    "last-week": "last-week",
    "last week": "last-week",
    "上周": "last-week",
    "this-month": "this-month",
    "this month": "this-month",
    "本月": "this-month",
    "这个月": "this-month",
}


def _help_text() -> str:
    return (
        "**/obsidian-review 用法**\n\n"
        "`/obsidian-review init-profile --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`/obsidian-review confirm-profile --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`/obsidian-review this-week --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`/obsidian-review --from 2026-06-01 --vault D:\\download\\Obsidian\\Jasmine`\n\n"
        "首次进入 Vault 必须先 init-profile，用户在 Obsidian 中维护 "
        "`Reviews/_AgentProfile/vault_profile.draft.md` 的用户确认区；首次建模后运行一次 confirm-profile 建立 baseline。"
    )


def is_obsidian_review_request(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s or s.startswith("/"):
        return False
    has_obsidian = "obsidian" in s or "vault" in s or "知识库" in s
    has_review = any(k in s for k in ("复盘", "周报", "总结", "review", "weekly"))
    return has_obsidian and has_review


def parse_request(text: str) -> dict[str, str]:
    body = (text or "").strip()
    command = ""
    rest = body
    if body:
        first, _sep, tail = body.partition(" ")
        if first in PROFILE_COMMANDS:
            command = first
            rest = tail.strip()
    lower = body.lower()
    rest_lower = rest.lower()

    vault = ""
    vault_patterns = [
        r"--vault\s+([^\n]+?)(?=\s+--|\s+(?:today|this-week|last-week|this-month|--from)\b|$)",
        r"(?:vault|Vault|VAULT)\s*(?:是|=|:|：)?\s*([A-Za-z]:\\[^\n]+?)(?=\s+(?:today|this-week|last-week|this-month|--from)\b|$)",
        r"([A-Za-z]:\\[^\n]+)",
    ]
    for pat in vault_patterns:
        m = re.search(pat, rest)
        if m:
            vault = m.group(1).strip().strip('"').strip("'")
            break

    from_date = ""
    m = re.search(r"(?:--from|from|since|从)\s*(\d{4}-\d{2}-\d{2})", rest, re.I)
    if m:
        from_date = m.group(1)

    period = ""
    for alias, value in PERIOD_ALIASES.items():
        if alias in rest_lower or alias in rest:
            period = value
            break
    if not period and not from_date and not command:
        period = "this-week"

    return {"command": command, "vault": vault, "period": period, "from_date": from_date, "raw": body}


def _run_helper(args: list[str]) -> tuple[bool, dict, str]:
    cmd = [sys.executable, SCRIPT_PATH] + args
    proc = subprocess.run(
        cmd,
        cwd=CODE_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    try:
        data = json.loads(proc.stdout or proc.stderr or "{}")
    except Exception:
        data = {}
    return proc.returncode == 0, data, combined.strip()


def _emit_or_return(message: str, display_queue=None) -> Optional[str]:
    if display_queue:
        display_queue.put({"done": message, "source": "system"})
        return None
    return message


def _ensure_init(vault: str, display_queue=None) -> tuple[bool, str]:
    if not vault:
        return True, ""
    config_path = os.path.join(vault, ".obsidian-review-agent", "config.json")
    if os.path.exists(config_path):
        return True, ""
    if display_queue:
        display_queue.put({"next": f"[obsidian-review] 初始化 Vault 配置: {vault}\n", "source": "system"})
    ok, data, log = _run_helper(["init", "--vault", vault])
    if ok:
        return True, ""
    return False, "初始化 Obsidian review 配置失败：\n" + (log or json.dumps(data, ensure_ascii=False))


def _profile_command(user_request: str, display_queue=None) -> Optional[str]:
    spec = parse_request(user_request)
    command = spec["command"]
    vault = spec["vault"]
    ok, err = _ensure_init(vault, display_queue)
    if not ok:
        return _emit_or_return(err, display_queue)

    helper_command = "profile-init" if command == "init-profile" else "profile-confirm"
    args = [helper_command]
    if vault:
        args += ["--vault", vault]
    if display_queue:
        action = "生成环境模型草案" if command == "init-profile" else "确认环境模型并建立初始 snapshot"
        display_queue.put({"next": f"[obsidian-review] 正在{action}...\n", "source": "system"})
    ok, data, log = _run_helper(args)
    if not ok:
        return _emit_or_return(log or json.dumps(data, ensure_ascii=False, indent=2), display_queue)

    if command == "init-profile":
        message = (
            "Obsidian 环境模型草案已生成。\n\n"
            f"- 草案路径：{data.get('profile_draft')}\n"
            f"- 扫描 Markdown 文件数：{data.get('markdown_files')}\n"
            "- 下一步：在 Obsidian 中直接维护 `Reviews/_AgentProfile/vault_profile.draft.md` 的“用户确认区”，"
            "首次建模完成后运行一次 `/obsidian-review confirm-profile --vault <path>` 建立初始 snapshot。"
        )
    else:
        message = (
            "Obsidian 环境模型已确认，初始 review snapshot 已按确认时 Vault 状态建立。\n\n"
            f"- confirmed profile：{data.get('confirmed_profile')}\n"
            f"- L1 context：{data.get('l1_context')}\n"
            f"- snapshot：{data.get('snapshot')}\n"
            f"- 扫描 Markdown 文件数：{data.get('markdown_files')}\n"
            "- 现在可以运行周期复盘命令，例如 `/obsidian-review this-week --vault <path>`。"
        )
    return _emit_or_return(message, display_queue)


def _prepare_review(user_request: str, display_queue=None) -> tuple[dict | None, str | None]:
    spec = parse_request(user_request)
    vault = spec["vault"]
    period = spec["period"]
    from_date = spec["from_date"]

    ok, err = _ensure_init(vault, display_queue)
    if not ok:
        return None, err

    args = ["prepare"]
    if vault:
        args += ["--vault", vault]
    if from_date:
        args += ["--from", from_date]
    else:
        args += ["--period", period or "this-week"]
    if display_queue:
        display_queue.put({"next": "[obsidian-review] 正在扫描 Vault 并生成复盘证据...\n", "source": "system"})
    ok, data, log = _run_helper(args)
    if not ok:
        return None, "prepare 执行失败：\n" + (log or json.dumps(data, ensure_ascii=False))
    if not data.get("changed_blocks_file") or not os.path.exists(data.get("changed_blocks_file", "")):
        return None, "prepare 未生成 changed_blocks.latest.json，输出为：\n" + json.dumps(data, ensure_ascii=False, indent=2)
    digest_file = data.get("review_digest_file", "")
    if not digest_file or not os.path.exists(digest_file):
        return None, "prepare 未生成 review_digest.latest.json，输出为：\n" + json.dumps(data, ensure_ascii=False, indent=2)
    return data, None



def render_report_prompt(user_request: str, prepared: dict) -> str:
    vault = prepared.get("vault_path") or parse_request(user_request).get("vault", "")
    review_id = prepared.get("review_id", "")
    run_dir = prepared.get("run_dir", "")
    changed_blocks_file = prepared.get("changed_blocks_file", "")
    review_digest_file = prepared.get("review_digest_file", "")
    state_update_file = prepared.get("review_state_update_file", "")
    memory_proposals_file = prepared.get("memory_proposals_file", "")
    suggested_report = prepared.get("suggested_report", "")
    profile_update_file = prepared.get("vault_profile_update_file", "")
    profile_draft = prepared.get("profile_draft", "")
    draft_candidates_added = prepared.get("profile_draft_candidates_added", 0)
    period = prepared.get("period", "")
    date_start = prepared.get("date_start", "")
    date_end = prepared.get("date_end", "")
    changed_files = prepared.get("changed_files", 0)
    changed_blocks = prepared.get("changed_blocks", 0)
    vault_arg = f' --vault "{vault}"' if vault else ""
    review_arg = f' --review-id "{review_id}"' if review_id else ""

    return f"""> Obsidian Review Skill -> GenericAgent 周期复盘

用户原始请求：{user_request}

prepare 已经完成；不要重新运行 prepare。所有收尾文件必须属于同一个 review run。

```json
{json.dumps(prepared, ensure_ascii=False, indent=2)}
```

本轮 review_id：`{review_id}`
本轮 run_dir：`{run_dir}`

必须按顺序执行：

1. 读取 `{SOP_PATH}`，确认报告结构。
2. 读取 `{review_digest_file}` 作为主要写作输入；它是本轮 run-scoped digest。
3. 只有证据不清时才读取 `{changed_blocks_file}` 查证细节；不要直接扫描整个 Vault。
4. 生成 Markdown 复盘报告并写入：`{suggested_report}`
5. 写入 run-scoped 状态文件 `{state_update_file}`，必须包含 `review_id: "{review_id}"`，并至少包含 `open_items`、`blockers`、`active_topics`。
6. 写入 run-scoped memory proposals 文件 `{memory_proposals_file}`。没有候选时也要写空数组：`{{"schema_version": 1, "review_id": "{review_id}", "proposals": []}}`
7. memory proposals 最多 5 条；每条最多 180 中文字符；只保存路径或 Obsidian link 作为 evidence，不保存原文。允许的 kind 只有 `mainline_progress`、`mainline_gap`、`mainline_next_step`、`new_mainline_candidate`、`workflow_preference_candidate`。
8. prepare 已经把自动候选写入 `{profile_update_file}`；如有可展示的新增候选，也可能已追加到 `{profile_draft}`。本次自动追加候选数：{draft_candidates_added}。不要改写 profile draft 的用户确认区。
9. 报告、状态文件、memory proposals 都写成功后运行 finalize：
   `python "{SCRIPT_PATH}" finalize{vault_arg}{review_arg} --report "{suggested_report}"`
10. 最后只向用户汇报 report path、review_id、period、changed/new files、changed blocks、profile draft candidates added、finalize 是否成功。

硬约束：
- `latest` files 只用于兼容和人工查看，不能作为 finalize 的事实来源。
- `vault_profile.draft.md` 用户确认区是文件夹用途、长期目标、活跃主线的最高优先级上下文。
- `memory_proposals.json` 只表示 pending candidate，不是 confirmed memory。
- 不要把报告全文或原文片段塞进 memory；memory 只保存少量结构化候选和证据指针。
- 报告优先基于 `{review_digest_file}` 的 `file_summaries`；`{changed_blocks_file}` 只是细节查证文件。
- 必须总结本周期所有新增/修改文件；每个有变化的用户主题都要形成一条逻辑线。
- 禁止把报告写成 `[[来源]]: 原文片段` 列表；必须按主题写逻辑串联总结。
- source_link 已经是 Obsidian 双链，原样使用，不要再套一层 `[[...]]`。
- 禁止保留 `[整体总结]`、`[待填内容]`、`[项目进展]` 之类占位符。
- 不要在报告、状态文件、memory proposals 全部写成功前 finalize。
- 报告要写回 Obsidian 的 `Reviews/`，不是只输出在对话里。"""


def render_prompt(user_request: str, display_queue=None) -> Optional[str]:
    spec = parse_request(user_request)
    if spec["command"] in PROFILE_COMMANDS:
        return _profile_command(user_request, display_queue)
    prepared, err = _prepare_review(user_request, display_queue)
    if err:
        return _emit_or_return(err, display_queue)
    return render_report_prompt(user_request, prepared or {})


def handle(agent, body: str, display_queue) -> Optional[str]:
    body = (body or "").strip()
    if body in ("help", "?", "-h", "--help"):
        display_queue.put({"done": _help_text(), "source": "system"})
        return None
    return render_prompt(body or "复盘我的 Obsidian 本周内容", display_queue)


def install(cls) -> None:
    if getattr(cls, "_obsidian_review_patched", False):
        return
    orig = cls._handle_slash_cmd

    def patched(self, raw_query, display_queue):
        s = (raw_query or "").strip()
        if s == "/obsidian-review":
            return handle(self, "", display_queue)
        if s.startswith("/obsidian-review ") or s.startswith("/obsidian-review\t"):
            return handle(self, s[len("/obsidian-review"):].strip(), display_queue)
        if is_obsidian_review_request(s):
            return render_prompt(s, display_queue)
        return orig(self, raw_query, display_queue)

    cls._handle_slash_cmd = patched
    cls._obsidian_review_patched = True
