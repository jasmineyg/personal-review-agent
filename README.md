# Personal Review Agent

一个面向个人 Obsidian 笔记的复盘助手。

它基于 [GenericAgent](https://github.com/lsdefine/GenericAgent) 的本地 Agent 能力改造而来，重点不是“再做一个聊天机器人”，而是帮你把一段时间内改过的 Obsidian 笔记整理成可以回看、可以继续推进的复盘报告。


## 它适合做什么

Personal Review Agent 适合这些场景：

- 你平时用 Obsidian 记录项目进展、论文阅读、面试准备、实验结论、日记或灵感。
- 你希望每周、每月或任意时间段回看一次最近改过的笔记。
- 你不想手动翻很多文件，只想让工具先把相关素材整理出来，再生成一篇可读的复盘。
- 你希望第一次使用时，工具先理解你的笔记目录和复盘偏好，以后生成内容更贴近你的习惯。

它会做三件事：

1. 先熟悉你的笔记库结构，并生成一份可检查的说明。
2. 按时间范围找到最近修改过的笔记，整理成复盘素材。
3. 把素材交给当前聊天生成复盘报告，并给出后续事项和回看入口。

## 界面预览

主界面以复盘流程为中心，右侧只保留一个辅助对话栏。你可以先整理素材，再开始写复盘。

![复盘工作台](docs/assets/screenshots/review-workspace.png)


点击“开始写复盘”后，内部任务会在后台发送；右侧不会展示大段 JSON 或工程指令，只显示生成过程和结果。

![复盘生成状态](docs/assets/screenshots/writing-status.png)

## 快速开始

推荐使用 Python 3.11 或 3.12。Python 3.10 到 3.13 也在项目配置范围内。

### 1. 安装

```powershell
git clone https://github.com/jasmineyg/personal-review-agent.git
cd Personal-review-agent
uv venv
uv pip install -e ".[ui]"
```

如果你不用 `uv`，也可以用普通虚拟环境安装：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[ui]"
```

### 2. 配置模型密钥

复制模板文件：

```powershell
copy mykey_template.py mykey.py
```

然后打开 `mykey.py`，填入你自己的模型 API Key 和模型配置。

不要把 `mykey.py` 提交到 Git。

### 3. 启动图形界面

```powershell
python launch.pyw
```

启动后浏览器会打开本地图形界面。默认地址类似：

```text
http://127.0.0.1:14168/
```

## 第一次使用

第一次使用时，请按界面里的引导操作：

1. 在“笔记库路径”里填写 Obsidian 笔记库的本地路径，例如 `D:\Obsidian\我的笔记`。
2. 点击“第一次使用：开始熟悉我的笔记库”。
3. 工具会生成一份说明文件，放在你的 Obsidian 笔记库里。
4. 打开这份说明，检查它对目录结构、长期主题和复盘偏好的理解是否准确。
5. 修改确认后，回到界面点击“我已检查，开始使用”。

完成后，之后就可以直接整理复盘素材。

## 日常复盘流程

1. 选择时间范围：今天、本周、上周、本月，或自定义日期。
2. 点击“整理复盘素材”。
3. 检查界面反馈：它会告诉你找到了多少个文件、多少条可用内容。
4. 点击“开始写复盘”。
5. 生成完成后，在右侧查看结果；报告保存位置也会显示在界面里。

当前筛选规则是按文件的“最后修改时间”选择复盘素材。也就是说，只要 Markdown 文件的最后修改时间落在你选择的日期范围内，就会进入本次复盘素材；`Reviews`、`.obsidian`、`.trash`、`.obsidian-review-agent` 等内部目录会被跳过。

## 常用命令

启动图形界面：

```powershell
python launch.pyw
```

启动终端界面：

```powershell
python frontends/tui_v3.py
```

检查命令行入口：

```powershell
ga --help
```

直接运行一次 Obsidian 复盘命令：

```powershell
python -m memory.obsidian_review.obsidian_review prepare --vault "D:\Obsidian\我的笔记" --period this-week
```

自定义日期：

```powershell
python -m memory.obsidian_review.obsidian_review prepare --vault "D:\Obsidian\我的笔记" --from 2026-07-01 --to 2026-07-15
```

## 文件会写到哪里

在你的 Obsidian 笔记库里，工具会创建这些目录或文件：

```text
.obsidian-review-agent/      内部状态和本次整理结果
Reviews/                    复盘报告默认保存位置
Reviews/_AgentProfile/      第一次使用时给你检查的说明
```

通常你只需要打开界面里显示的文件，不需要手动翻内部目录。

## 隐私说明

这个项目优先在本地运行：

- 笔记扫描、时间筛选、文件整理都发生在你的电脑上。
- `mykey.py` 只保存在本地，请不要提交。
- 生成复盘时，整理后的复盘素材会发送给你在 `mykey.py` 中配置的模型服务，用来生成报告。
- 带有 `#private`、`#secret`、`#ignore-review`、`#no-review` 的笔记会被跳过。
- 默认跳过 `.obsidian`、`.trash`、`.obsidian-review-agent` 和 `Reviews` 目录。

如果某些笔记不希望进入复盘，可以给它们加上忽略标签，或调整 `.obsidian-review-agent/config.json` 里的忽略规则。

## 常见问题

### 为什么找不到复盘素材？

先确认这几点：

- 选择的日期范围是否正确。
- 文件的最后修改时间是否落在这个范围内。
- 文件是否是 Markdown 文件。
- 文件是否在默认跳过的目录里，例如 `Reviews` 或 `.obsidian`。
- 文件里是否带有忽略标签，例如 `#private` 或 `#ignore-review`。

### 为什么要先让我检查说明？

因为每个人的 Obsidian 目录都不一样。第一次使用时，工具会先整理它对你笔记结构的理解。你检查一次后，以后的复盘会更稳定，也更贴近你自己的分类和表达方式。

### 右侧对话栏是做什么的？

右侧对话栏用于显示复盘生成过程和最终结果，也可以继续补充要求。内部任务参数不会作为用户消息展示出来。

### 可以不用图形界面吗？

可以。你可以使用 `frontends/tui_v3.py`，也可以直接调用 `memory/obsidian_review/obsidian_review.py` 里的命令。

## 开发者检查

修改代码后，可以先跑这些检查：

```powershell
python -m py_compile agent_loop.py agentmain.py ga.py llmcore.py frontends\tui_v3.py frontends\desktop_bridge.py frontends\obsidian_review_cmd.py memory\obsidian_review\obsidian_review.py
node --check frontends\desktop\static\app.js
ga --help
```

## 和 GenericAgent 的关系

Personal Review Agent 是基于 GenericAgent 的一个聚焦版本。原项目强调极简 Agent 内核、自我沉淀能力和本地执行能力；这个分支保留了这些基础能力，并把主要体验收束到“个人 Obsidian 复盘”这个具体工作流上。

上游项目：<https://github.com/lsdefine/GenericAgent>

## License

MIT


