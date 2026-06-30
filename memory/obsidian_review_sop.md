# Obsidian Review SOP

> GenericAgent Obsidian 周期复盘 Skill。目标是把 Obsidian Vault 中一个周期内
> 的新增 / 修改内容，整理成带来源回链、主题归类、阻塞追踪和下周期继承事项的
> Markdown 复盘报告，并写回 Vault。

---

## 一、何时使用

用户要求类似下面任务时启用：

- `review my obsidian this week`
- `复盘我的 Obsidian 本周内容`
- `生成今天 / 本月 / 从 2026-06-01 开始的 Obsidian 复盘`
- `/obsidian-review this-week --vault D:\download\Obsidian\Jasmine`

这个 SOP 是 GenericAgent 内部 Skill，不是 Obsidian 插件。GenericAgent 通过
`frontends/obsidian_review_cmd.py` 识别自然语言和 `/obsidian-review` 命令，
然后在自己的 agent loop 中调用工具完成 `prepare -> 写报告 -> finalize`。
确定性脚本只负责扫描、过滤、切块、diff、摘要生成和状态提交；报告正文由当前
GenericAgent 对话中的 LLM 优先根据 `review_digest.latest.json` 生成，必要时再查证
`changed_blocks.latest.json`。

---

## 二、文件位置

```text
memory/obsidian_review_sop.md
memory/obsidian_review/obsidian_review.py
memory/obsidian_review/config.example.json
```

Vault 初始化后会创建：

```text
Vault/.obsidian-review-agent/
  config.json
  changed_blocks.latest.json
  review_digest.latest.json
  pending_snapshot.latest.json
  review_snapshot.json
  review_state.json
  review_state_update.latest.json
Vault/Reviews/
  YYYY-MM-DD_<period>_周期复盘.md
```

---

## 三、快速命令

### GenericAgent 内部调用

在 GenericAgent CLI / TUI / 桌面前端里直接输入：

```text
复盘我的 Obsidian 本周内容，Vault 是 D:\download\Obsidian\Jasmine
```

或使用显式命令：

```text
/obsidian-review this-week --vault D:\download\Obsidian\Jasmine
```

GenericAgent 会把它转换为本 SOP 的执行任务，自己运行 prepare、读取
`review_digest.latest.json`，必要时用 `changed_blocks.latest.json` 查证来源，写报告、
写 `review_state_update.latest.json`，最后 finalize。

### 底层脚本命令

初始化：

```bash
python memory/obsidian_review/obsidian_review.py init --vault "D:\download\Obsidian\Jasmine"
```

准备本周复盘：

```bash
python memory/obsidian_review/obsidian_review.py prepare --vault "D:\download\Obsidian\Jasmine" --period this-week
```

准备从某天到现在的复盘：

```bash
python memory/obsidian_review/obsidian_review.py prepare --vault "D:\download\Obsidian\Jasmine" --from 2026-06-01
```

报告写回后提交状态：

```bash
python memory/obsidian_review/obsidian_review.py finalize --vault "D:\download\Obsidian\Jasmine" --report "D:\download\Obsidian\Jasmine\Reviews\2026-06-30_this-week_周期复盘.md"
```

---

## 四、执行协议

### 步骤 1：读取配置或初始化

如果用户给了 Vault 路径，优先使用该路径。若未初始化，先运行 `init`。
如果用户没有给 Vault 路径，可以让脚本从本机 Obsidian 配置发现 Vault；
发现多个 Vault 时需要用户指定。

### 步骤 2：运行 prepare

按用户要求选择周期：

| 用户表达 | 参数 |
|---|---|
| 今天 / today | `--period today` |
| 本周 / this week | `--period this-week` |
| 上周 / last week | `--period last-week` |
| 本月 / this month | `--period this-month` |
| 从某天开始 / since / from | `--from YYYY-MM-DD` |

`prepare` 会：

1. 读取 `.md` 文件；
2. 跳过隐私目录和隐私标签文件；
3. 解析 Markdown 结构块；
4. 和上次 snapshot 做块级 diff；
5. 写出 `.obsidian-review-agent/changed_blocks.latest.json`；
6. 写出 `.obsidian-review-agent/review_digest.latest.json`；
7. 写出 `.obsidian-review-agent/pending_snapshot.latest.json`。

### 步骤 3：读取 review digest

GenericAgent 主要读取：

```text
Vault/.obsidian-review-agent/review_digest.latest.json
```

只有在需要补充证据、核对 `source_link` 或检查原始上下文时，才读取：

```text
Vault/.obsidian-review-agent/changed_blocks.latest.json
```

不要让 LLM 直接扫整个 Vault，也不要把隐私过滤前的 Markdown 原文交给 LLM。

### 步骤 4：生成报告

报告必须用下面固定骨架，主题名称由证据动态生成，不写死。

```markdown
---
type: obsidian-review
period: ...
date_start: ...
date_end: ...
run_mode: first_baseline | block_diff
source: GenericAgent
changed_blocks_file: .obsidian-review-agent/changed_blocks.latest.json
review_digest_file: .obsidian-review-agent/review_digest.latest.json
---
# 本周期复盘
## 1. 本周期工作总览
## 2. 本周期完成 / 产出事项
## 3. 按主题分类复盘
### 主题 A
#### 逻辑串联总结
#### 相关来源
### 主题 B
## 4. 项目 / 目标进展
## 5. 未解决问题与阻塞事项
## 6. 下周期继承事项
## 7. 建议与下一步计划
## 8. 来源与关联笔记
```

写作规则：

1. 不按文件机械罗列，要按主题串联本周期实际推进脉络。
2. 关键结论尽量带 `source_link`，使用 Obsidian 双链。
3. “完成 / 产出事项”分清楚明确完成和内容产出。
4. 有阅读笔记可以说“学习 / 整理了”，不要说“完全掌握了”。
5. 有项目构思可以说“形成初步方案”，不要说“完成项目”。
6. 未完成 checkbox、TODO、blocked、疑问和反复出现的问题要进入阻塞或继承事项。
7. 首次运行时 `run_mode=first_baseline`，报告要说明这是基于本周期修改文件形成的基线复盘，不声称所有内容都是本周期新增。
8. 禁止写成 `[[来源]]: 原文片段` 的逐条搬运清单；来源链接只作为证据引用，正文要先综合再引用。
9. 禁止保留占位符，例如 `[整体工作方向和主要进展的简要总结]`。

### 章节要求

- **本周期工作总览**：一段话说明主要方向，以及这些方向之间的关系。
- **本周期完成 / 产出事项**：列出明确完成事项和实际内容产出。
- **按主题分类复盘**：自动识别主题，允许跨文件夹合并或拆分。
- **项目 / 目标进展**：结合长期主题、活跃项目、配置偏好和历史状态判断进展。
- **未解决问题与阻塞事项**：整理未完成任务、疑惑、风险、blocked 和持续问题。
- **下周期继承事项**：把未完成任务、待复习内容和开放问题变成候选计划。
- **建议与下一步计划**：给出可执行下一步。
- **来源与关联笔记**：集中列出关键来源。

### 报告文件名

写入 `config.output_dir`，默认 `Reviews/`。同一天同周期不覆盖，自动加序号：

```text
2026-06-30_this-week_周期复盘.md
2026-06-30_this-week_周期复盘_2.md
```

### review_state_update.latest.json

报告写回后，同时生成：

```text
Vault/.obsidian-review-agent/review_state_update.latest.json
```

建议结构：

```json
{
  "open_items": [],
  "blockers": [],
  "active_topics": []
}
```

不要保存整篇报告。只保存下一次复盘需要延续的结构化状态。

### 步骤 5：运行 finalize

报告文件成功写回 Obsidian 后再运行 `finalize`。`finalize` 会：

1. 校验报告文件存在；
2. 把 `pending_snapshot.latest.json` 提交为 `review_snapshot.json`；
3. 合并 `review_state_update.latest.json` 到 `review_state.json`；
4. 写入 `latest_report` 和 `last_run`。

如果报告生成失败，不要运行 `finalize`。

---

## 五、隐私规则

默认跳过目录：

```text
.obsidian/
.trash/
.obsidian-review-agent/
Reviews/
```

默认跳过包含以下标签的整篇文件：

```text
#private
#secret
#ignore-review
#no-review
```

第一版不做块级隐私过滤。发现隐私标签时整篇文件跳过，宁可保守漏掉一些内容，
也不要把私密上下文送入 LLM。

---

## 六、changed_blocks 字段

每个 block 至少包含：

```json
{
  "block_id": "...",
  "status": "added | modified | deleted | continued",
  "type": "paragraph | list | todo | heading | code",
  "file": "AI-Agent/example.md",
  "top_dir": "AI-Agent",
  "parent_dirs": ["AI-Agent", "obsidian review agent"],
  "heading_path": ["项目计划", "第一版"],
  "start_line": 18,
  "end_line": 21,
  "text": "...",
  "text_hash": "...",
  "source_link": "[[AI-Agent/example#第一版]]",
  "candidate_activity": "project_planning"
}
```

`candidate_activity` 只是脚本给 LLM 的粗提示，不是最终主题。

---

## 七、验收清单

完成后至少验证：

1. `init` 能创建配置、状态目录和报告目录；
2. 隐私目录和隐私标签文件会被跳过；
3. 首次运行能生成 `first_baseline` changed blocks；
4. 第二次运行能识别 `added / modified / deleted / continued`；
5. changed blocks 包含文件、标题路径、行号、source link、candidate activity；
6. `review_digest.latest.json` 包含主题摘要、未完成事项、阻塞事项和来源索引；
7. 报告主要基于 review digest 综合生成，而不是逐条搬运 changed blocks；
8. 报告写回路径不覆盖旧报告；
9. `finalize` 后更新 snapshot 和 review state；
10. `python -m py_compile memory/obsidian_review/obsidian_review.py` 通过。
