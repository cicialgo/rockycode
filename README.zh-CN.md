<div align="center">

<img src="brand/rockycode-wordmark.svg" alt="rockycode" width="420">

<br>

**一个可以对话的编程智能体引擎**<br>
极简核心、DeepSeek V4 适配、研究模式搜索增强、面向自进化的实验特性，以及量化 harness 的一键 bench 测试。

[English](README.md) · [简体中文](README.zh-CN.md)

![SWE-bench Verified](https://img.shields.io/badge/SWE--bench_Verified-88.8%25_V4--flash_GA-7d5cc6)
![V4-pro preview](https://img.shields.io/badge/V4--pro_preview-74.9%25-8d6cd0)
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

完整 500 题的成绩：**独立的完整 500 题运行**，每个模型各 3 轮，所有模型使用完全相同的 harness 与配置（步数上限 100、单次最大输出 32,768 token、推理力度 `max`、thinking 开启），由官方 SWE-bench harness 打分，未针对任务做任何调优。注意版本差异：`deepseek-v4-flash` 一列是**正式版（GA，V4-Flash-0731）**；而 `deepseek-v4-pro` 的三轮在 GA 版 V4-Pro-0813 发布之前运行，该列是 **preview 版**成绩 —— flash 与 pro 的差距里含有版本差，不是同代对比。

| 轮次 | `deepseek-v4-flash`（GA） | `deepseek-v4-pro`（preview） | `minimax-m3` |
|---|---|---|---|
| 第 1 轮 | 90.0%（450/500） | 75.6%（378/500） | 72.8%（364/500） |
| 第 2 轮 | 88.6%（443/500） | 74.8%（374/500） | 71.2%（356/500） |
| 第 3 轮 | 87.8%（439/500） | 74.4%（372/500） | 70.0%（350/500） |
| **平均** | **88.8%** | **74.9%** | **71.3%** |
| 多轮并集（pass@3） | 95.4%（477/500） | 81.8%（409/500） | 83.6%（418/500） |

两行汇总要分开读。**平均值**是可以与排行榜对比的数字 —— 每一轮都是独立的单次完整 500 题。**并集**是 pass@3：至少被某一轮解出的任务。两者之间的差距（DeepSeek 两个模型各约 7 个点、MiniMax 约 12 个点）是轮次间方差，不是能力上限 —— 模型在这套 harness 下已经"够得着"这些任务，只是无法每一轮都稳住。当前的工作重心就是收掉这个差距：finish 前的验证门控与多轮选择，而不是继续改提示词。作为参照：DeepSeek 用自家 scaffold 报告 V4 Pro (Preview) 为 80.6%。逐轮拆解请关注我们的X账号（@rockycode_ai）。

另有两轮较早的 flash-preview 运行（79.8% —— 即此前列在这里的那一轮 —— 与 81.2%）在运行途中遭遇本地网络中断，表现为连续任务块返回空补丁；这两轮已用干净的重跑替换，未计入平均。

我们计划支持DeepSWE bench，目前还在调试中。

## 快速开始

环境要求：Python 3.11+（经 [uv](https://docs.astral.sh/uv/) 管理）和一个OpenAI 兼容的 API key（默认提供商是 DeepSeek）。对 chat 及 research/learn模式而言，不需要 Docker。

**Docker Desktop** 仅在需要容器隔离工具执行的模式下才必需：`goal`（自主运行）、`exec`（自动化委托）、`bench`（SWE-bench 打分），以及 chat 里可选的`/sandbox`。这些模式在沙箱内默认离线运行，被委托或无人值守的任务因此无法触碰你的主机、无法访问网络。

```bash
uv tool install rockycode     # 推荐 —— `rockycode` 命令直接进 PATH
rockycode                     # 首次运行会引导你完成 API key 设置
```

已经装过、想升到新版本？用 **`uv tool upgrade rockycode`** ——
注意重复执行 `uv tool install` 不会升级：它看到已有安装就静默保留旧版本。

还没有 uv？一条命令安装：
`curl -LsSf https://astral.sh/uv/install.sh | sh`（Windows 及其他方式见
[uv 安装文档](https://docs.astral.sh/uv/getting-started/installation/)）。

三种装法看着像，落点完全不同：

- **`uv tool install rockycode`**（推荐）—— 给 CLI 一个独立的隔离环境，并把
  `rockycode` 命令放上 PATH；系统 Python 低于 3.11 时，uv 会自动拉取一个匹配
  的解释器。想「当应用装」就用它。
- **`uv pip install rockycode`** —— 只装进**当前激活的虚拟环境**：`rockycode`
  命令只存在于那个 venv 里，新开一个 shell 会找不到它（要么先激活 venv，
  要么用 `uv run rockycode` 运行）。
- **`pip install rockycode`** —— 同样只进当前环境，且要求 Python 3.11+。在更老
  的 Python 上会报一个很有误导性的
  `ERROR: No matching distribution found for rockycode`。原因：pip 只会提供
  `requires-python` 与你解释器匹配的版本，老 Python 下它一个可装的版本都看
  不到，于是把「版本不满足」报成了「包不存在」。遇到这个错不用纠结 —— 直接用
  上面的 `uv tool install rockycode`，uv 自带 3.11+ 的 Python。

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
| `/effort off\|low\|high\|max` | 推理深度，会话内实时可调（按位置收敛到各提供商自己的档位） |
| `/permission yolo\|ask\|careful` | 本次会话的工具审批严格度 —— 不带参数打开选择器；也可按 `shift+tab` 循环切换，或点击状态栏的 🔒 标记 |
| `/sandbox on\|off\|status` | 把工具执行隔离进容器 |
| `/lsp` | 语言服务器状态；诊断信息随 `read_file` 一并返回 |
| `/artifact` | 本会话的 artifact：`list` · `open <n>` · `stop` · `live on\|off` |
| `/paste` | 粘贴剪贴板图片（或 `ctrl+v`）；无视觉模型自选识图路由 |
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

DeepSeek 是主场模型，但**模型是数据而非代码**：整份目录都在
`rockycode/models.toml` 里 —— 每个提供商一个国内 base URL、一个 key 名、
一种推理参数形状；每个模型有自己的上下文窗口、最大输出、视觉标记、价格与
角色。引擎只读这份 spec，自身不带任何模型专属数字，所以新增一个模型只是改
数据。你自己的 `~/.rockycode/models.toml`（同样形状）会深度合并在上面：加
模型、改上限、补价格、隐藏某一行。

| 提供商 | 模型（❖ = 支持图片输入） | 上下文 / 最大输出 |
|---|---|---|
| **deepseek**（默认） | `deepseek-flash`（V4.1 Flash，默认）❖、`deepseek-v4-pro` | 1M / 384K |
| **glm** | `glm-5.3`、`glm-5.3-flash` ❖ | 1M / 128K |
| **kimi** | `kimi-k3` ❖ | 1M / 128K |
| **minimax** | `minimax-m3` ❖ | 1M / 128K |
| **stepfun** | `step-5-preview` ❖ | 1M / 64K |
| **qwen** | `qwen3.8-max` ❖、`qwen3.8-flash` ❖ | 1M / 64K |
| **mimo** | `mimo-v2.6-pro` ❖ | 1M / 128K |
| **ollama**（本地，$0） | 你拉取过什么就是什么 —— 从运行中的服务实时发现 | 以服务端实测为准 |

每个提供商只有一个国内端点（key 名 `ROCKYCODE_<提供商>_API_KEY`；旧的
`_CN_`/`_EN_` 名字仍然能读）。自带 URL 与 key 的订阅套餐单独成行 ——
`qwen-plan`（百炼 Token Plan）、`mimo-plan`、`stepfun-plan` —— key 名为
`ROCKYCODE_<提供商>_PLAN_API_KEY`。`/model` 选择器**先选模型**（每个模型
一行）；选中的模型如果有多个端点，再问由哪个 URL 提供服务（官方 · 套餐 ·
你自己的），其中「custom base URL」一行可以填你自己的网关或代理，按提供商
记住（`~/.rockycode/endpoints.toml`，以 `<提供商>-custom` 寻址）。直接输入
spec 可以跳过这一切：`/model glm:flash`、`/model qwen-plan:qwen3.8-max`。已
下线的 `deepseek-v4-flash` / `-vision-exp` 名字仍可解析（落到
`deepseek-flash`，与 DeepSeek 自己的路由一致）。选择器只展示已配置好 key
的提供商。

视觉能力按「模型」区分：`deepseek-flash` 用主 key 就能识图，默认会话贴图即
用。纯文本模型（`deepseek-v4-pro`、`glm-5.3`）收到的贴图会由
`deepseek-flash` 静默描述（`image_route auto`），或交给你自己的 CLI。
`rockycode config model <spec>` 把任意选择固化为启动默认。上下文窗口与最大
输出跟随当前模型（配置 `context_window` / `max_tokens` = `0`）；填一个数字
则是你自己的上限，切换模型也不变。DeepSeek 与 MiniMax 都有完整 500 题的
bench 成绩（见上方「能力量化」）；其余提供商属于[实验性功能](#实验性功能)。

推理深度旋钮（`/effort off|low|high|max`）与提供商无关；各提供商自己的档位
来自注册表，旋钮在请求层按位置收敛到它们上面（StepFun 的
`low|medium|high` 里 rocky 的 `high` 对应 `medium`；GLM 与 Kimi 关不掉
thinking，`off` 会发它们的最低档）。`xhigh` 仍然接受，等于 `max`。

rocky 也能配置它自己：告诉它「用我的代理」「加上我的 vLLM 服务」「换默认
模型」，内置的 `rocky-setup` 技能加上需要确认的 `rocky_config` 工具就会在
`~/.rockycode` 下完成修改。唯一不碰的是 key —— 它只告诉你变量名，值由你
自己粘贴。

#### 本地模型（Ollama）

rocky 集成的是 OpenAI 兼容*协议*，而不是某个运行时 —— 推荐
[Ollama](https://ollama.com)（0.19 起在 Apple Silicon 上用 MLX 引擎）。
无需 key、无需配置：`ollama serve` 跑起来，拉一个支持工具调用的模型
（推荐并实测过 `ollama pull qwen3.8:27b-mlx`），它就会出现在 `/model`
里 —— 列表是实时发现的、按 `$0 · local` 计价。

切到本地模型前会先跑一遍**就绪预检** —— 服务在不在、模型拉没拉、支不支持
工具调用、serving 上下文够不够 —— 有问题就拒绝切换，并给出确切的修复命令
（`ollama pull …`、`export OLLAMA_CONTEXT_LENGTH=65536`）。最要紧的一条：
Ollama 默认上下文很小且**静默截断**，agent 会话会以莫名其妙的方式挂掉 ——
请用 `OLLAMA_CONTEXT_LENGTH=65536` 启动。每次切换 rocky 都会把自己的
`context_window` 对齐到服务端已验证的值。

其它本地服务（LM Studio、llama.cpp、vLLM）同样只是数据：在
`~/.rockycode/providers.toml` 里给提供商加一行 `local = true`，
它的端点就免 key。

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
调用而设计。stdout 是 JSONL：一行 `meta`、模型的 `text`、以及带证据（改过
的文件、跑过的命令、被拒的操作）的 `result` 信封 —— 只给证据不给结论，由
调用方自己核验；`--events` 会补上逐工具的回执行。预算始终强制生效；退出码
区分成功、失败、待审批、预算终止 —— 调用方因此可以补上一次授权后继续，而
不必猜测。

用 `--profile` 决定 rocky 能做多少：`read`（read_file / grep / glob /
view_image —— 无 shell、不写文件）和 `write`（多了限制在 `--workdir` 内的
write_file / edit_file）直接在主机上运行，**不需要 Docker**，秒开 —— 这正是
Claude Code 或 Codex 想要的「看看这个仓库告诉我」或小改动委托，用便宜又快
的模型跑。`full` 加上 bash，**默认**在 Docker 沙箱内（命令分类器只是纵深防
御，不是边界）。

```bash
rockycode exec --profile read "重试逻辑在哪个模块里，被哪些地方调用？"
rockycode exec --profile write "给 utils.py 里每个公开函数补上 docstring"
```

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
- **DeepSeek 以外的提供商。** GLM、Kimi、MiniMax、StepFun、Qwen、MiMo 都以
  OpenAI 兼容的注册表条目接入（`/model`）。DeepSeek 与 MiniMax 已有完整
  bench 成绩（见「能力量化」）；其余在拿到 bench 分数前请当作未验证。
  `models.toml` 里标着 `note = "… verify …"` 的条目（MiniMax 的端点域名、
  MiMo 的鉴权头、各套餐 URL）取自 2026-09-29 各家文档，尚未实际调用 ——
  错了也只是改一行数据。

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