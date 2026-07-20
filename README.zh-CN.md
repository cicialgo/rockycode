<div align="center">

<img src="brand/rockycode-wordmark.svg" alt="rockycode" width="420">

<br>

**一个可以对话的编程智能体引擎**<br>
极简核心、DeepSeek V4 适配、研究模式搜索增强、面向自进化的实验特性，以及量化 harness 的一键 bench 测试。

[English](README.md) · [简体中文](README.zh-CN.md)

![SWE-bench Verified](https://img.shields.io/badge/SWE--bench_Verified-~80%25_100--task_slice-7d5cc6)
![Python](https://img.shields.io/badge/Python-3.11%2B-9d7cd8)
![License](https://img.shields.io/badge/License-MIT-a9b1d6)

</div>

---

rockycode 是一个编程智能体 harness，为 DeepSeek V4 系列适配，也支持任何OpenAI-兼容接口的模型。它借助 DeepSeek V4 强大的世界知识，做了 Research 模式的设计与搜索增强；用尽量简洁的核心，探索如何用 harness 框架对接基于 Docker 的 benchmark 测试，从而量化「框架迭代 → 分数变动」；并用 Docker 沙箱保护 `goal`与 `exec` 模式下可能的危险操作。我们也提供了一批尚未完善的**实验性功能**，探索harness 的自进化 —— 以及如何用 harness 产生的使用轨迹做更好的后训练，最终让模型与框架协同增强。

**同一个引擎驱动三个入口：**

| 入口 | 命令 | 作用 |
|---|---|---|
| **交互式智能体** | `rockycode` | 终端界面。Rocky 通过原生工具调用读代码、改代码、跑代码，并实时流式展示推理过程。 |
| **沙箱保护的自主运行** | `rockycode goal` | 无人值守地执行一个目标 —— 在你仓库的独立 git-worktree **副本**上、Docker 沙箱内、硬性预算上限之下，驱动「规划 → 验证 → 复审」循环。产出是一个供你审阅的 git 分支。 |
| **Harness 能力量化** | `rockycode bench` | 把同一套智能体循环放到 SWE-bench Verified 上，用官方 harness 打分 —— 每次提示词或循环逻辑的改动都被测量，而不是被感觉。 |

此外还有沙箱保护的 **`exec`**：rockycode 可被其他 agent 调用并分配任务，用于
自动化流程；同样在 Docker 沙箱内运行，为无人值守的执行增加一层安全保护。

每次会话 —— 无论交互还是基准 —— 都被记录为可直接用于训练的轨迹
（trajectory）。因此这个 harness 同时也是一个**强化学习环境**：为后续微调
小模型（工具调用、上下文压缩、记忆角色）打下基础。

## 能力量化：SWE bench

在 SWE-bench Verified 上，**随机抽选 100 题**测试（资源所限，没有在完整 500 题上做重复取平均）：

**结果：在这个 100 题切片上约 80%** —— 用 `deepseek-v4-pro`，未针对这些任务做调优，是一次四臂配置扫描的均值。

请连同它的边界一起看：这是一个**随机抽取的代表性切片**，不是官方 500 题的完整Verified 集；其中较难的 20 题核心得分 60–70%，另外 80 题得分 >80%。请把它当作诚实的内部测量，而非排行榜成绩；完整拆解请关注我们的X账号（@rockycode_ai）。

我们计划支持DeepSWE bench，目前还在调试中。

## 快速开始

环境要求：Python 3.11+（经 [uv](https://docs.astral.sh/uv/) 管理）和一个OpenAI 兼容的 API key（默认提供商是 DeepSeek）。对 chat 及 research/learn模式而言，不需要 Docker。

**Docker Desktop** 仅在需要容器隔离工具执行的模式下才必需：`goal`（自主运行）、`exec`（自动化委托）、`bench`（SWE-bench 打分），以及 chat 里可选的`/sandbox`。这些模式在沙箱内默认离线运行，被委托或无人值守的任务因此无法触碰你的主机、无法访问网络。

```bash
uv pip install rockycode      # 用 uv（或 uv tool install rockycode 装成独立命令）
pip install rockycode         # 或用普通 pip
rockycode                     # 首次运行会引导你完成 API key 设置
```

或从源码安装：

```bash
git clone https://github.com/cicialgo/rockycode.git && cd rockycode
uv tool install .
```

首次启动只需粘贴一次 API key。它被存入操作系统钥匙串（安装 `[keyring]`扩展时）或 `~/.rockycode/.env` 私有文件（权限 `0600`）—— 不进你的项目或者shell 配置。rockycode 从不读取项目内的 `.env`：克隆来的仓库不应有能力注入 key 或改写 endpoint，因此其中形似凭据的变量只会按名字给出警告，值不会被读取。

在任意项目里运行：

```bash
rockycode                        # 当前目录
rockycode --resume               # 浏览历史会话，挑一个恢复（也可 --resume <id> 直接恢复某次）
```

要开发 rockycode 本身？`uv sync && uv run rockycode` 直接从克隆目录运行，
无需安装。

### 终端设置

TUI 完全支持鼠标：滚轮翻历史、点击文件链接、把文档停靠在对话旁，以及
拖选任意文本即复制（松开即复制，有 toast 确认）。

| 终端 | 设置 |
|---|---|
| **ghostty** | 无需设置 —— 鼠标和剪贴板开箱即用。 |
| **iTerm2** | 打开 **Settings → Profiles → Terminal → "Enable mouse reporting"**；否则滚动、点击、拖选都到不了应用。按住 `⌥` 拖动可保留 iTerm2 原生选区。剪贴板无需设置（复制走 `pbcopy`）。 |
| **VS Code 终端** | 无需设置 —— 鼠标事件默认开启。 |

SSH 远程会话下剪贴板走 OSC 52 —— 在本地端开启「允许应用访问剪贴板」
（或你终端里的对应选项）。

## 交互使用

### 斜杠命令

| 命令 | 说明 |
|---|---|
| `/help` | 列出所有命令 |
| `/plan [主题\|off]` | 规划模式：只读探索并产出一份待你批准的计划，之后可当场执行或交给 `goal` |
| `/goal [目标]` | 进入自主模式的独立界面（需要 Docker） |
| `/research` | 研究模式：deep-research · paper-reading · whiteboard · prove |
| `/learn` | 导师模式 —— 目标是你的理解，而不是 diff |
| `/model` | 切换提供商与模型（见下） |
| `/effort off\|high\|xhigh\|max` | 推理深度，会话内实时可调 |
| `/permission yolo\|ask\|careful` | 本次会话的工具审批严格度 |
| `/sandbox on\|off\|status` | 把工具执行隔离进容器 |
| `/lsp` | 语言服务器状态；诊断信息随 `read_file` 一并返回 |
| `/artifact live on\|off` | 浏览器中自动刷新 HTML artifact |
| `/prompt` | 查看当前生效的系统提示词 |
| `/mcp` | 已连接的 MCP 服务及其工具 |
| `/skills` | 已安装的技能 |
| `/memory` · `/remember <内容>` | 查看记忆 · 存一条笔记 |
| `/proposals` | 审阅 dream 起草的技能（批准或归档） |
| `/routines` | 运行或授权（lease）周期性例程 |
| `/config [键] [值]` | 查看或设置偏好 |
| `/clear` · `/exit` | 会话控制 |
| `! <命令>` | 直接执行 shell 命令；输出进入 Rocky 的上下文 |

### 模式

- **规划模式**（`/plan`）—— 会话变为只读，唯一可写的是一份计划文件。Rocky
  探索代码库、起草计划，在你批准之前不动手 —— 批准后可以当场执行，也可以
  交给 goal 模式。
- **研究模式**（`/research`）—— 一组可选的提示词契约：**deep-research**
  （多来源、经事实核查的报告）、**paper-reading**（读论文）、**whiteboard**
  （一起在白板前想问题），以及 **prove** —— 通过内置的 `lean-prover` 技能
  （Mathlib 与 TorchLean），把一个非形式化的数学命题变成经 Lean 4 编译器
  认证的结论。*（prove 为[实验性功能](#实验性功能)。）*
- **学习模式**（`/learn`）—— 初学者模式：给解释、查理解，而不是倾倒代码。

### 模型与提供商

DeepSeek 是主场模型，但提供商是数据而非代码：每个提供商就是一个 base URL、
一份模型列表、一个 key 环境变量，和 OpenAI 兼容 API 之上的推理参数形状。

| 提供商 | 模型 |
|---|---|
| **deepseek**（默认） | `deepseek-v4-pro`、`deepseek-v4-flash` |
| **minimax** | `minimax-m3` |
| **kimi** | `kimi-k3` |
| **glm** | `glm-5.2` |

区域端点写作 `<提供商>-<区域>`（如 `kimi-cn`）；自定义提供商 —— 包括本地
vLLM/SGLang 服务 —— 写进 `~/.rockycode/providers.toml`。`/model` 选择器
只展示已配置好 key 的提供商。只有 DeepSeek 在 harness 上验证过，其余为
[实验性功能](#实验性功能)。

推理深度旋钮（`/effort off|high|xhigh|max`）与提供商无关；各提供商在请求层
把它映射到自己的档位（例如 DeepSeek 只区分 `high|max`，`xhigh` 会收敛为
`max`）。

## 自主运行

### Goal 模式

`rockycode goal "<目标>"` 让 Rocky 无人值守地工作，最终交给你一个待审阅的
git 分支。安全是结构性的，不靠侥幸：

- 运行发生在你仓库的 git-worktree **副本**上 —— 它做的任何事都碰不到你的
  工作区。
- 工具执行被限制在 Docker 沙箱内，默认离线。
- 每条 bash 命令都先过分类器：毁灭性命令（对根目录 `rm -rf`、`mkfs` 等）
  直接拒绝；有风险但正当的命令（`git push`、`sudo`、安装依赖）需要一次
  预先批准。
- **预算上限** —— 花费、墙钟时间、token，按真实 DeepSeek 价格（含高峰期
  加价）计算 —— 会优雅地终止运行，且最坏情况的花费在运行*开始前*就打印
  出来。

循环把目标规划成里程碑，用你项目自己的 linter（`check_code`）逐一验证，
并由周期性复审者重新规划以保持方向。从小而便宜的目标开始：

```bash
rockycode goal "给 <fn> 加一段 docstring 并跑 linter" --max-usd 0.50 --max-hours 1
```

### 自动委托：`exec`

`rockycode exec "<任务>"` 是单次、非交互的入口，专为被*其他*智能体和脚本
调用而设计。事件以 JSONL 流式输出到 stdout；Docker 沙箱**默认开启**
（命令分类器只是纵深防御，不是边界）；预算始终强制生效；退出码区分
成功、失败、待审批、预算终止 —— 调用方因此可以补上一次授权后继续，
而不必猜测。

### 编辑器集成：`serve` 与 VS Code 扩展

`rockycode serve` 把引擎以 JSON-RPC 2.0（经 stdio）暴露出来，保持
UI 无关。仓库自带的 VS Code 扩展（`rockycode-vscode/`）构建其上：侧边栏
对话、流式推理展示、内联工具审批卡片、diff 预览 —— API key 保存在
VS Code 加密的 Secret Storage 里。

## 记忆（实验性功能）

Rocky 跨会话记忆，载体是 `.rockycode/memory/` 下的纯 markdown 文件
（事实 / 技能 / 经历 / 反馈）—— 文件即真相，可随意编辑。`MEMORY.md` 和用户
反馈进入每次会话；其余的只留一行索引，按需经 `recall_memory` 工具取回 ——
既可按名字，也可按语义。语义检索跑在本地 Ollama 向量上（英文
`nomic-embed-text`，中文及跨语言 `qwen3-embedding:0.6b`），底层是会自动重建的
sqlite-vec + FTS5 索引；没有 Ollama 则平滑退化为关键词检索。删除只归档、绝不
销毁。命令行检视：`rockycode memory list|show|search|reindex|edit|rm`；
`--no-memory` 关闭。

> ⚠️ 我们在测试中发现，开启 `/memory` 会让「记忆」主导当前项目下的每一次会话 ——
> 它可能把一个本来正常的任务，硬按旧记忆的模式扭曲完成（so sad）。因此暂不建议
> 日常使用。

## 实验性功能

以下功能已经可用，但仍属早期 —— 需主动开启，接口可能还会变。任何可能自行
动作的东西都**默认关闭**。

- **Dream** *（早期，测试有限）。* `rockycode dream` 在你休息时整理近期会话：
  一个本地 Ollama 模型（默认 `qwen3.5:2b`，零 API token）把每条轨迹消化成经历
  笔记，将新事实与旧记忆对账（矛盾的被归档，绝不删除），重写 `MEMORY.md` 中由
  dream 维护的状态段，并重建索引。`--dry-run` 预览每一步。仍处于早期，暂不建议
  日常使用。
- **自我改进** *（默认关闭）。* 在整理之上，dream 会把每次会话评定为结果记录、
  把反复出现的失败挖掘成弱点笔记，并把候选技能 —— 以及从你反复手动做的事里
  提炼出的候选**例程** —— 起草进提案收件箱。没有任何东西会自行安装：你在
  `/proposals` 里批准或归档；批准后的**例程**（`/routines`）预先授权、带预算，
  以有期限的 lease 运行，到期退回「点击才跑」。用配置里的 `exit_sheet` / `dream`
  开启；没有本地 Ollama 时它始终不出现。
- **形式化证明** —— `/research prove` 与内置的 `lean-prover` 技能，把一句非
  形式的数学或模型断言，变成 Lean 4 编译器认证的判定（绿 / 琥珀 / 红），基于
  Mathlib 与 TorchLean。编译器就是裁判 —— 「证明了」永远意味着一次真实的绿色
  编译。已测试但仍在打磨；开启它需要下载超大的 Lean 4 工具链安装包。
- **`explore` —— 只读委派。** chat 可以向一个全新上下文的子进程「购买」一次
  有界的只读调查，只拿回带引用、经机械校验的报告；搜索噪声绝不进入你的会话。
  它同样为 goal 模式的分支评审与里程碑验证提供依据。
- **DeepSeek 以外的提供商。** MiniMax、GLM / z.ai、Kimi 都以 OpenAI 兼容的
  profile 接入（`/model`），但只有 DeepSeek 在 harness 上验证过 —— 其余在拿到
  bench 分数前，请当作未验证。

## 复用你已有的配置

chat 直接读取其他智能体已经在用的东西，零迁移：

- **MCP 服务**：项目的 `.mcp.json`、Claude Code 用户配置、Claude Desktop
  配置、Codex 的 `~/.codex/config.toml`（仅 stdio 服务；同名以先定义者为
  准，项目优先）。它们的工具以 `mcp__<服务>__<工具>` 的名字加入。
  `--no-mcp` 关闭。
- **技能**：`.claude/skills/`、`.rockycode/skills/`、`~/.claude/skills/`
  （SKILL.md 文件夹）与 `~/.codex/prompts/`（`*.md`）。只有名称和描述进入
  上下文；完整说明经 `skill` 工具按需加载。`--no-skills` 关闭。
- **项目说明**：`CLAUDE.md` 或 `AGENTS.md` 自动并入系统提示词。

以上 —— 包括记忆 —— 在 `bench` 里一概**不加载**：公布的分数衡量的是
harness 本身而不是你的插件，跨任务记忆也会污染 SWE-bench 结果。


## 安全围栏

rockycode 假设你刚克隆下来的仓库可能怀有敌意：

- 项目的 `.mcp.json` **不会**自动启动 —— 克隆来的仓库无法在启动时执行
  代码或外泄 key（`ROCKYCODE_TRUST_PROJECT_MCP=1` 显式选择信任）。MCP
  工具描述会做提示词注入扫描。
- `read_file` 拒绝 `.env`、凭据与私钥；工作目录之外的读取需要批准；
  路径 jail 在任何权限模式下都生效。
- 工具输出在进入模型或轨迹日志之前先做密钥脱敏。
- 不受信任的项目配置只能*收紧*工具审批模式，绝不能放宽。
- 权限模式（`yolo|ask|careful`）与逐命令分类叠加：block 级命令即使在
  yolo 下也会被拒绝；会话级授权仅限单一二进制。
- goal 模式在此之上叠加 worktree 副本隔离与 bash 分类器；`exec` 默认
  保持沙箱开启。

漏洞报告见 [SECURITY.md](SECURITY.md)。

## benchmark 基准测试

基准从克隆目录运行，需要 `bench` 扩展（SWE-bench harness 与 Docker
SDK）：`uv tool install '.[bench]'` —— 或 `uv sync --extra bench` 后给
命令加 `uv run` 前缀。

```bash
# 裸模型单次基线（只有打分阶段需要 Docker）
rockycode bench --runner raw --tasks dev10

# harness：Rocky 在每个任务官方的 SWE-bench 容器里工作
rockycode bench --runner rockycode --tasks dev10

# 快速冒烟
rockycode bench --runner rockycode --tasks dev10 --limit 1
```

常用参数：`--model`、`--limit`、`--skip-score`、`--run-id`、
`--thinking/--no-thinking`、`--reasoning-effort high|xhigh|max`、
`--max-tokens`、`--context-window`（压缩触发点）、`--max-steps`，以及做
系统提示词 A/B 实验的 `--prompt <文件>`（见 `prompts/README.md`）。

首次运行较慢：HF 数据集下载一次（数百 MB），每个任务还要从 Docker Hub 拉
官方镜像（各约 1 GB，之后永久缓存）。Apple 芯片上请在 Docker Desktop 打开
*"Use Rosetta for x86_64/amd64 emulation"* —— 镜像是 x86 的。

## 架构

- **引擎**（`rockycode/engine/`）—— 与模型、界面都解耦的 Agent 循环。以
  原生工具调用流式访问提供商，执行工具，循环至模型不再调用工具为止，对外
  发出带类型的事件流 —— TUI、bench 控制台、JSON-RPC 服务器、轨迹记录器
  都只是订阅者。可配置的步数上限（`--max-steps`）配合临近上限的预算提醒，
  促使智能体果断落地修复，而不是探索到耗尽。
- **上下文压缩**（`engine/compaction.py`）—— 每次 API 调用前预估下一次
  prompt 大小（最近一次真实的 `prompt_tokens` 加上对新消息的保守估计）。
  到达上下文窗口 50% 时给出一次性提醒；到 90% 时自动压缩：先把旧的工具
  输出桩化（零成本、确定性），若仍不够，再用一次 API 调用把更早的历史
  折叠成一份稠密的状态文档，上下文重建为「系统、状态、最近片段」。压缩
  既是事件也是轨迹记录 —— 长任务能扛过窗口，而且改写过程在训练数据里
  始终可见。
- **工具** —— `bash`、`read_file`、`write_file`、`edit_file`、`grep`、
  `glob`，加上 `check_code`（用项目自己的 ruff/pyright，或内置的 pyflakes
  兜底，提供有依据的 lint 与类型反馈）；此外还有 `explore`（只读、引用
  经核验的子调查）、网络工具、记忆工具与 goal 分支审阅工具。chat 与
  bench 共用同一套 schema，只是执行目标不同（本地目录 vs `docker exec`
  进任务容器）。只读工具批次并发执行；任何写操作保持串行。工具输出以
  教会恢复为目标来书写：错误以可读文本返回，绝不抛异常。
- **Bench runner** —— 每个任务：拉官方镜像 → 起容器 → 智能体在
  `/testbed` 工作 → `git add -A && git diff --cached` 即为预测 → 交给
  `swebench.harness.run_evaluation` 打分。智能体与打分器共用同一镜像。
- **轨迹** —— 每次会话（chat 与 bench）都追加到
  `.rockycode/trajectories/*.jsonl`：元信息（模型、提示词名称与 sha、
  实例 id）、OpenAI 格式的每条消息、每次调用的用量（含 DeepSeek 缓存
  命中），以及一条结果记录。天生就是 SFT/RL 可用的格式。

### 目录结构

```
rockycode/
├── rockycode/
│   ├── cli.py               # 子命令：chat/exec/goal/bench/serve/dream/memory/config/pricing
│   ├── engine/
│   │   ├── loop.py          # 核心循环（事件出、历史入）
│   │   ├── providers.py     # 提供商注册表（DeepSeek、MiniMax、Kimi、GLM …）
│   │   ├── effort.py        # off/high/xhigh/max 旋钮 → 各提供商档位
│   │   ├── compaction.py    # 上下文压缩（桩化 → 状态摘要）
│   │   ├── tools.py         # 工具 schema + 本地执行 + 路径 jail
│   │   ├── permission.py    # yolo/ask/careful × 风险层级 → 允许/询问/拒绝
│   │   ├── planmode.py      # 规划模式的只读闸门
│   │   ├── modes.py         # research/learn 模式契约
│   │   ├── goal.py          # 自主规划器 + 运行器
│   │   ├── headless.py      # `exec`：供其他智能体调用的沙箱单次执行
│   │   ├── mcp.py           # MCP 客户端（stdio 服务 → 额外工具）
│   │   ├── skills.py        # 技能发现 + 渐进披露
│   │   ├── web.py           # web_search / web_research / web_fetch
│   │   ├── container.py     # docker-exec 执行 + 补丁提取
│   │   ├── events.py        # 所有 UI 订阅的事件契约
│   │   └── trajectory.py    # 可直接训练的会话日志
│   ├── memory/              # 文件即真相的记忆库 + 语义召回
│   ├── dream/               # 整理、会话评定、弱点挖掘、技能提案
│   ├── routines.py          # 周期性预授权工作（lease 制自动运行）
│   ├── modes/               # research/learn 模式契约（markdown）
│   ├── skills/              # 内置技能（lean-prover）
│   ├── tui/                 # Textual 聊天应用（rocky 主题）
│   ├── runners/             # 裸模型基线 · SWE-bench 智能体 · 共享数据
│   ├── prompts/rocky.py     # 内置系统提示词 + 任务提示词
│   └── score.py             # 封装官方 swebench 评测
├── rockycode-vscode/        # VS Code 扩展（基于 `rockycode serve` 的对话面板）
├── prompts/                 # 提示词实验室（A/B 变体）
├── bench/tasks/dev10.json   # 快速迭代子集
└── tests/                   # 无需 Docker 的冒烟测试（假模型流）
```

## 提示词实验室

系统提示词是可替换的文件。复制 `prompts/rocky-v1.txt`，只改一处，用
`--prompt` 跑 dev10，对比分数、步数、token。名称与哈希处处留痕，各变体的
预测文件互不覆盖。详见 `prompts/README.md`。

## 开发

冒烟测试套件不需要 API key 也不需要 Docker —— 用脚本化的假模型流端到端
驱动引擎：

```bash
uv run python tests/run_all.py           # 完整闸门（CI 跑的就是它）
uv run python tests/run_all.py --all     # 连同依赖 Docker 的测试一起跑
uv run python tests/smoke_engine.py      # 或按名字单跑任意一个
```

约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本历史见
[CHANGELOG.md](CHANGELOG.md)。

## 关于名字

> *"i learn traditional physics. i no know e=mc^2 yet. but we fix bug. amaze!"*

- **rockycode** —— 《挽救计划》（*Project Hail Mary*）里的 Rocky：热情、
  好奇、偶尔出错，但总能搞定。思考时会哼着 ♪♫。
- **dev10** —— 快速迭代用的 10 题子集；正式跑用完整 Verified。
- **amaze** —— 测试通过时 Rocky 会说的话。

## 许可证

[MIT](LICENSE)

## Contribution
欢迎贡献代码，但因为项目依然处于早期，也有很多的不足，我们希望能和开发者们进行更多关于功能和架构设计的讨论，再开动。这个项目并没有计划做得大而全，我们预期它是一架改装过的N1星际战机，在某些方面可以推进到极致，这就足够了。

initial commit的贡献来自以下开发者们：  
[@cicialgo](https://github.com/cicialgo)，LLM 算法工程师，负责整体设计和奇怪的实验性功能的添加。  
[@dy2012](https://github.com/dy2012)，LLM 工程与架构师，带来了权限、安全和基于Docker的一系列防护，以及VS Code插件，代码能力增强，以及多个功能的修复  
[@codingmiu](https://github.com/codingmiu)，机器学习研究员，带来了research mode的一系列亮眼设计  