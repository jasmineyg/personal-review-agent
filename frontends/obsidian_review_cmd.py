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
from datetime import date, datetime, timedelta
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
        "**/obsidian-review \u7528\u6cd5**\n\n"
        "`/obsidian-review init-profile --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`/obsidian-review confirm-profile --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`/obsidian-review this-week --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`/obsidian-review --from 2026-06-01 --to 2026-06-15 --vault D:\\download\\Obsidian\\Jasmine`\n"
        "`帮我总结 Obsidian 最近 3 天 --vault D:\\download\\Obsidian\\Jasmine`\n\n"
        "\u9996\u6b21\u8fdb\u5165 Vault \u5148\u8fd0\u884c init-profile\uff0c\u6253\u5f00\u751f\u6210\u7684 `Reviews/_AgentProfile/vault_profile.draft.md`\uff0c"
        "\u6309\u4f60\u7684\u771f\u5b9e\u7406\u89e3\u4fee\u6539\u201c\u6211\u7684\u590d\u76d8\u8bb0\u5fc6\u201d\uff1b\u9996\u6b21\u6574\u7406\u5b8c\u6210\u540e\u8fd0\u884c\u4e00\u6b21 confirm-profile \u5efa\u7acb baseline\u3002"
    )


def is_obsidian_review_request(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s or s.startswith("/"):
        return False
    has_obsidian = "obsidian" in s or "vault" in s or "知识库" in s
    has_review = any(k in s for k in ("复盘", "周报", "总结", "review", "weekly"))
    return has_obsidian and has_review


_DATE_TOKEN = (
    r'(?:\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}[日号]?)?'
    r'|\d{1,2}月\d{1,2}[日号]?'
    r'|\d{1,2}[-/]\d{1,2}'
    r'|今天|今日|昨天|昨日|前天)'
)

_CN_NUM = {
    '零': 0,
    '一': 1,
    '二': 2,
    '两': 2,
    '三': 3,
    '四': 4,
    '五': 5,
    '六': 6,
    '七': 7,
    '八': 8,
    '九': 9,
}


def _cn_int(value: str) -> int | None:
    value = (value or '').strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value in _CN_NUM:
        return _CN_NUM[value]
    if value == '十':
        return 10
    if '十' in value:
        left, _sep, right = value.partition('十')
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def _date_from_token(token: str, today: date) -> date | None:
    token = (token or '').strip()
    if token in ('今天', '今日'):
        return today
    if token in ('昨天', '昨日'):
        return today - timedelta(days=1)
    if token == '前天':
        return today - timedelta(days=2)

    m = re.fullmatch(r'(\d{4})[-/.年](\d{1,2})(?:[-/.月](\d{1,2})[日号]?)?', token)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3) or 1)
        return date(year, month, day)

    m = re.fullmatch(r'(\d{1,2})月(\d{1,2})[日号]?', token)
    if m:
        return date(today.year, int(m.group(1)), int(m.group(2)))

    m = re.fullmatch(r'(\d{1,2})[-/](\d{1,2})', token)
    if m:
        return date(today.year, int(m.group(1)), int(m.group(2)))

    return None


def _natural_date_range(text: str) -> tuple[str, str]:
    s = text or ''
    today = datetime.now().date()

    m = re.search(rf'({_DATE_TOKEN})\s*(?:到|至|~|～|—|--|\s+-\s+|\s+to\s+|\s+until\s+)\s*({_DATE_TOKEN})', s, re.I)
    if m:
        start = _date_from_token(m.group(1), today)
        end = _date_from_token(m.group(2), today)
        if start and end:
            return start.isoformat(), end.isoformat()

    m = re.search(r'(?:最近|近|过去|past|last)\s*([0-9一二两三四五六七八九十]+)\s*(天|日|周|星期|礼拜|个月|月|days?|weeks?|months?)', s, re.I)
    if m:
        n = _cn_int(m.group(1)) or 1
        unit = m.group(2).lower()
        if unit in ('周', '星期', '礼拜', 'week', 'weeks'):
            days = n * 7
        elif unit in ('个月', '月', 'month', 'months'):
            days = n * 30
        else:
            days = n
        return (today - timedelta(days=max(days - 1, 0))).isoformat(), today.isoformat()

    if re.search(r'(?:最近|近|过去|past|last)\s*(?:一)?(?:周|星期|礼拜|week)', s, re.I):
        return (today - timedelta(days=6)).isoformat(), today.isoformat()

    if re.search(r'(?:最近|近|过去|past|last)\s*(?:一)?(?:个月|月|month)', s, re.I):
        return (today - timedelta(days=29)).isoformat(), today.isoformat()

    if re.search(r'上个?月|last\s+month', s, re.I):
        year, month = today.year, today.month - 1
        if month == 0:
            year, month = today.year - 1, 12
        start, end = _month_bounds(year, month)
        return start.isoformat(), end.isoformat()

    m = re.search(r'(\d{4})年\s*(\d{1,2})月', s)
    if m:
        start, end = _month_bounds(int(m.group(1)), int(m.group(2)))
        return start.isoformat(), end.isoformat()

    m = re.search(r'(?<!\d)(\d{1,2})月(?!\d|[日号])', s)
    if m:
        start, end = _month_bounds(today.year, int(m.group(1)))
        return start.isoformat(), end.isoformat()

    for word in ('前天', '昨天', '昨日', '今天', '今日'):
        if word in s:
            day = _date_from_token(word, today)
            if day:
                return day.isoformat(), day.isoformat()

    return '', ''


def parse_request(text: str) -> dict[str, str]:
    body = (text or '').strip()
    command = ''
    rest = body
    if body:
        first, _sep, tail = body.partition(' ')
        if first in PROFILE_COMMANDS:
            command = first
            rest = tail.strip()
    rest_lower = rest.lower()

    vault = ''
    vault_patterns = [
        r'--vault\s+([^\n]+?)(?=\s+--|\s+(?:today|this-week|last-week|this-month|--from|--to|from|since|to|until|从|到|至|最近|近|过去|上个?月)\b|$)',
        r'(?:vault|Vault|VAULT)\s*(?:是|=|:|：)?\s*([A-Za-z]:\\[^\n]+?)(?=\s+(?:today|this-week|last-week|this-month|--from|--to|from|since|to|until|从|到|至|最近|近|过去|上个?月)\b|$)',
        r'([A-Za-z]:\\[^\n]+)',
    ]
    for pat in vault_patterns:
        m = re.search(pat, rest)
        if m:
            vault = m.group(1).strip().strip(chr(34)).strip(chr(39))
            break

    from_date = ''
    m = re.search(r'(?:--from|from|since|从)\s*(\d{4}-\d{2}-\d{2})', rest, re.I)
    if m:
        from_date = m.group(1)
    to_date = ''
    m = re.search(r'(?:--to|to|until|到|至)\s*(\d{4}-\d{2}-\d{2})', rest, re.I)
    if m:
        to_date = m.group(1)

    if not from_date:
        from_date, to_date = _natural_date_range(rest)
    elif not to_date:
        natural_from, natural_to = _natural_date_range(rest)
        if natural_from == from_date and natural_to:
            to_date = natural_to

    period = ''
    if not from_date:
        for alias, value in PERIOD_ALIASES.items():
            if alias in rest_lower or alias in rest:
                period = value
                break
    if not period and not from_date and not command:
        period = 'this-week'

    return {'command': command, 'vault': vault, 'period': period, 'from_date': from_date, 'to_date': to_date, 'raw': body}


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
        action = "\u751f\u6210\u590d\u76d8\u8bb0\u5fc6" if command == "init-profile" else "\u786e\u8ba4\u590d\u76d8\u8bb0\u5fc6\u5e76\u5efa\u7acb\u521d\u59cb snapshot"
        display_queue.put({"next": f"[obsidian-review] 正在{action}...\n", "source": "system"})
    ok, data, log = _run_helper(args)
    if not ok:
        return _emit_or_return(log or json.dumps(data, ensure_ascii=False, indent=2), display_queue)

    if command == "init-profile":
        message = (
            "Obsidian \u590d\u76d8\u8bb0\u5fc6\u5df2\u751f\u6210\u3002\n\n"
            f"- \u6587\u4ef6\u8def\u5f84\uff1a{data.get('profile_draft')}\n"
            f"- \u626b\u63cf Markdown \u6587\u4ef6\u6570\uff1a{data.get('markdown_files')}\n"
            "- \u4e0b\u4e00\u6b65\uff1a\u5728 Obsidian \u4e2d\u6253\u5f00\u8fd9\u4efd\u201c\u6211\u7684\u590d\u76d8\u8bb0\u5fc6\u201d\uff0c\u6309\u4f60\u7684\u771f\u5b9e\u7406\u89e3\u4fee\u6539\u6587\u4ef6\u5939\u7528\u9014\u548c\u957f\u671f\u4e3b\u7ebf\uff1b"
            "\u9996\u6b21\u6574\u7406\u5b8c\u6210\u540e\u8fd0\u884c\u4e00\u6b21 `/obsidian-review confirm-profile --vault <path>` \u5efa\u7acb\u521d\u59cb snapshot\u3002"
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
    vault = spec['vault']
    period = spec['period']
    from_date = spec['from_date']
    to_date = spec['to_date']

    ok, err = _ensure_init(vault, display_queue)
    if not ok:
        return None, err

    args = ['prepare']
    if vault:
        args += ['--vault', vault]
    if from_date:
        args += ['--from', from_date]
        if to_date:
            args += ['--to', to_date]
    else:
        args += ['--period', period or 'this-week']
    if display_queue:
        display_queue.put({'next': '[obsidian-review] 正在扫描 Vault 并生成复盘证据...\n', 'source': 'system'})
    ok, data, log = _run_helper(args)
    if not ok:
        return None, 'prepare 执行失败：\n' + (log or json.dumps(data, ensure_ascii=False))
    if not data.get('changed_blocks_file') or not os.path.exists(data.get('changed_blocks_file', '')):
        return None, 'prepare 未生成 changed_blocks.latest.json，输出为：\n' + json.dumps(data, ensure_ascii=False, indent=2)
    digest_file = data.get('review_digest_file', '')
    if not digest_file or not os.path.exists(digest_file):
        return None, 'prepare 未生成 review_digest.latest.json，输出为：\n' + json.dumps(data, ensure_ascii=False, indent=2)
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

    finalize_code_lines = [
        "import subprocess, sys",
        f'args = [sys.executable, r"{SCRIPT_PATH}", "finalize"]',
    ]
    if vault:
        finalize_code_lines.append(f'args += ["--vault", r"{vault}"]')
    if review_id:
        finalize_code_lines.append(f'args += ["--review-id", r"{review_id}"]')
    finalize_code_lines += [
        f'args += ["--report", r"{suggested_report}"]',
        "subprocess.run(args, check=True)",
    ]
    finalize_code = "\n".join(finalize_code_lines)

    return f"""> Obsidian Review Skill -> GenericAgent 周期复盘

用户原始请求：{user_request}

prepare 已经完成；不要重新运行 prepare。你只负责基于本轮 evidence 写回报告、状态文件和 memory proposals，然后 finalize。所有收尾文件必须属于同一个 review run。

```json
{json.dumps(prepared, ensure_ascii=False, indent=2)}
```

本轮 review_id：`{review_id}`
本轮 run_dir：`{run_dir}`
周期：`{period}` `{date_start}` 至 `{date_end}`
变动文件数：`{changed_files}`；辅助定位块数：`{changed_blocks}`

必须按顺序执行：

1. 读取 `{SOP_PATH}`，确认写作方向。
2. 读取 `{review_digest_file}` 作为主要写作输入；它是本轮 run-scoped digest。
3. 只有证据不清时才读取 `{changed_blocks_file}` 查证细节；不要直接扫描整个 Vault。
4. 生成 Markdown 复盘报告并写入：`{suggested_report}`
5. 写入 run-scoped 状态文件 `{state_update_file}`，必须包含 `review_id: "{review_id}"`，并至少包含 `open_items`、`blockers`、`active_topics`。
6. 写入 run-scoped memory proposals 文件 `{memory_proposals_file}`。没有候选时也要写空数组：`{{"schema_version": 1, "review_id": "{review_id}", "proposals": []}}`
7. memory proposals 最多 5 条；每条最多 180 中文字符；只保存路径或 Obsidian link 作为 evidence，不保存原文。允许的 kind 只有 `mainline_progress`、`mainline_gap`、`mainline_next_step`、`new_mainline_candidate`、`workflow_preference_candidate`。
8. prepare 已经把自动候选写入 `{profile_update_file}`；如有可展示的新增候选，也可能已追加到 `{profile_draft}`。本次自动追加候选数：{draft_candidates_added}。不要改写 profile draft 的用户确认区。
9. 报告、状态文件、memory proposals 都写成功后，用 `code_run` 运行下面这段 Python 完成 finalize；不要把 shell 命令直接填进 `code_run` 脚本里。

```python
{finalize_code}
```

写作方向：
- 报告是给用户看的复盘文章，不是程序运行日志。不要在报告中出现 `review_id`、`run_dir`、`changed_blocks`、`review_digest`、`file_summaries`、`finalize`、`latest`、`first_baseline` 等工程词。
- 标题和文件名都要自然可读，例如 `2026-07-07 至 2026-07-13 复盘`；不要把 hash 或内部编号写进用户可见内容。
- `vault_profile.draft.md` 用户确认区是文件夹用途、长期目标、活跃主线的最高优先级上下文。
- 请根据用户文件夹意图，帮助用户更好地使用这个文件夹中的内容；不要套固定分类模板，也不要把下面的示例当成规则。
- 少样本启发：如果内容像论文笔记，可以帮用户串联问题、方法差异和复习顺序；如果像网页剪藏或待读资料池，可以筛出值得细读的材料并说明价值；如果像项目记录，可以提炼实验、实现、错误验证、想法和下一步。这些只是示例，遇到其他用途要自行泛化判断。
- 对用户文档做总结提炼，输出洞察、关系、取舍、复习线索和行动建议；禁止把原有内容换一种说法逐段复述。
- 同一主题下多个文件要合成一条逻辑线，只保留少量关键 Obsidian 双链作为回看入口。
- 报告、状态文件、memory proposals 全部写成功后再 finalize。
- 最后只向用户简要汇报报告路径、周期范围和 finalize 是否成功。"""


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
