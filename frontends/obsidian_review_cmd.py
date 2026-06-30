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
SOP_REL = "memory/obsidian_review_sop.md"
SCRIPT_PATH = os.path.join(CODE_ROOT, "memory", "obsidian_review", "obsidian_review.py")
SOP_PATH = os.path.join(CODE_ROOT, "memory", "obsidian_review_sop.md")

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
        "`/obsidian-review this-week --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`/obsidian-review today --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`/obsidian-review --from 2026-06-01 --vault D:\\download\\Obsidian\\Jasmine`\n\n"
        "也可以直接说自然语言：\n"
        "`复盘我的 Obsidian 本周内容，Vault 是 D:\\download\\Obsidian\\Jasmine`\n\n"
        "GenericAgent 会自己执行 prepare、生成报告、写回 Reviews、finalize。"
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
    lower = body.lower()

    vault = ""
    vault_patterns = [
        r"--vault\s+([^\n]+?)(?=\s+--|\s+(?:today|this-week|last-week|this-month|--from)\b|$)",
        r"(?:vault|Vault|VAULT)\s*(?:是|=|:|：)?\s*([A-Za-z]:\\[^\n]+?)(?=\s+(?:today|this-week|last-week|this-month|--from)\b|$)",
        r"([A-Za-z]:\\[^\n]+)",
    ]
    for pat in vault_patterns:
        m = re.search(pat, body)
        if m:
            vault = m.group(1).strip().strip('"').strip("'")
            break

    from_date = ""
    m = re.search(r"(?:--from|from|since|从)\s*(\d{4}-\d{2}-\d{2})", body, re.I)
    if m:
        from_date = m.group(1)

    period = ""
    for alias, value in PERIOD_ALIASES.items():
        if alias in lower or alias in body:
            period = value
            break
    if not period and not from_date:
        period = "this-week"

    return {"vault": vault, "period": period, "from_date": from_date, "raw": body}


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


def _prepare_review(user_request: str, display_queue=None) -> tuple[dict | None, str | None]:
    spec = parse_request(user_request)
    vault = spec["vault"]
    period = spec["period"]
    from_date = spec["from_date"]

    if vault:
        config_path = os.path.join(vault, ".obsidian-review-agent", "config.json")
        if not os.path.exists(config_path):
            if display_queue:
                display_queue.put({"next": f"[obsidian-review] 初始化 Vault: {vault}\n", "source": "system"})
            ok, data, log = _run_helper(["init", "--vault", vault])
            if not ok:
                return None, "初始化 Obsidian review 配置失败：\n" + (log or json.dumps(data, ensure_ascii=False))

    args = ["prepare"]
    if vault:
        args += ["--vault", vault]
    if from_date:
        args += ["--from", from_date]
    else:
        args += ["--period", period or "this-week"]
    if display_queue:
        display_queue.put({"next": "[obsidian-review] 正在扫描 Vault，并生成 changed_blocks.latest.json 与 review_digest.latest.json...\n", "source": "system"})
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
    changed_blocks_file = prepared.get("changed_blocks_file", "")
    review_digest_file = prepared.get("review_digest_file", "")
    suggested_report = prepared.get("suggested_report", "")
    period = prepared.get("period", "")
    date_start = prepared.get("date_start", "")
    date_end = prepared.get("date_end", "")
    changed_blocks = prepared.get("changed_blocks", 0)
    vault_arg = f' --vault "{vault}"' if vault else ""

    return f"""> Obsidian Review Skill -> GenericAgent 自己执行周期复盘

用户原始请求：
{user_request}

GenericAgent 已经完成确定性准备步骤，prepare 输出如下：

```json
{json.dumps(prepared, ensure_ascii=False, indent=2)}
```

现在只剩写作和收尾，不要重新运行 prepare。

必须按顺序执行：

1. 读取 `{SOP_PATH}`，确认报告结构。
2. 读取 `{review_digest_file}` 作为主要写作输入。
3. 只有当 digest 的来源不够清楚时，才读取 `{changed_blocks_file}` 查证，不要直接扫描整个 Vault。
4. 按 SOP 的固定章节生成 Markdown 复盘报告，写入：
   `{suggested_report}`
5. 在 Vault 的 `.obsidian-review-agent/review_state_update.latest.json` 写入轻量状态更新，至少包含：
   - `open_items`
   - `blockers`
   - `active_topics`
6. 报告写成功后运行 finalize：
   `python "{SCRIPT_PATH}" finalize{vault_arg} --report "{suggested_report}"`
7. 最后只向用户汇报：
   - 报告路径
   - 本次周期和时间范围：{period}，{date_start} 到 {date_end}
   - changed blocks 数量：{changed_blocks}
   - 是否 finalize 成功

硬约束：
- 隐私过滤必须发生在 LLM 读内容前；写报告优先基于 `review_digest.latest.json`。
- `review_digest.latest.json` 是主要输入；`changed_blocks.latest.json` 是完整证据文件，不要逐条搬运。
- 禁止把报告写成 `[[来源]]: 原文片段` 列表；必须按主题写逻辑串联总结。
- source_link 已经是 Obsidian 双链，原样使用，不要再套一层 `[[...]]`。
- 禁止保留 `[整体总结]`、`[待填写]`、`[项目进展]` 之类占位符。
- 不要在报告写成功前 finalize。
- 报告要写回 Obsidian 的 `Reviews/`，不是只输出在对话里。
- 关键结论尽量使用 changed block 里的 Obsidian `source_link`。
"""


def render_prompt(user_request: str, display_queue=None) -> Optional[str]:
    prepared, err = _prepare_review(user_request, display_queue)
    if err:
        if display_queue:
            display_queue.put({"done": err, "source": "system"})
            return None
        return err
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
