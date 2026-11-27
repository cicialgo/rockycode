<div align="center">

<img src="brand/rockycode-wordmark.svg" alt="rockycode" width="420">

<br>

**和你一起构建的编程与科研智能体**<br>
极简引擎，多模型原生适配，终端与桌面客户端共用一个 engine。<br>
每次改动都可以被测量：SWE-bench Verified 已支持，DeepSWE 接入中，更多长程任务计划中。

[English](README.md) · [简体中文](README.zh-CN.md)

![SWE-bench Verified](https://img.shields.io/badge/SWE--bench_Verified-88.8%25_V4--flash_GA-7d5cc6)
![V4-pro preview](https://img.shields.io/badge/V4--pro_preview-74.9%25-8d6cd0)
![Python](https://img.shields.io/badge/Python-3.11%2B-9d7cd8)
![License](https://img.shields.io/badge/License-MIT-a9b1d6)

</div>

---

RockyCode 是一个和你一起构建的智能体 harness，面向编程与科研。你定方向，Rocky 读代码、改代码、跑代码、查资料、读论文、检查证明，推理过程实时展示在你面前。它为多个模型做了原生适配：DeepSeek V4 系列、Kimi K3 系列、GLM、MiniMax、MiMo 等，同时支持任何 OpenAI 兼容接口的模型。

RockyCode 也是一套 **harness benchmark**：在你仓库里工作的同一套循环，原样放到基准任务上打分。固定模型，改一处提示词或循环逻辑，分数的变化就是这次改动的效果。

| 基准 | 状态 |
|---|---|
| **SWE-bench Verified** | 已支持：完整 500 题，官方 harness 打分（[成绩](#能力量化swe-bench)） |
| **DeepSWE** | 接入中 |
| **更多长程任务（long-horizon）** | 计划中 |

**Rocky 的独特之处：**

- **一起构建** —— `/plan` 先探索，和你一起讨论、修订计划，你批准之后才动手。whiteboard 模式陪你把一个问题想清楚。需要你决定的步骤会成为一张审批卡片；在 `yolo` 模式下，只有你能批准的步骤留在 `/parked` 里等你。
- **科研** —— Research 模式：Deep Research 写多来源、经事实核查的报告；paper-reading 陪你读论文；`prove` 把一句数学断言交给 Lean 4 编译器检查（实验性）。内置的 `architecture-viz` 技能把一个神经网络模块画成三个互相联动的视图：论文结构图、张量形状、你的 PyTorch 代码。Lean 4 是 Rocky 的第一个科学工具，更多科学任务在计划中。
- **多模型，以及用来选模型的竞技场** —— 每个模型自己的推理参数、上下文上限、视觉能力、价格和订阅套餐，都写在一份数据（`models.toml`）里，引擎按每个模型自己的方式调用它。桌面客户端里的 Model Race（田忌赛马）：五个模型、同一个任务、五条互相隔离的匿名赛道（A–E）。保存你的评分（或点击揭晓）后，模型名字出现；终点榜按真实完成顺序回放比赛。用你自己的任务来选模型。
- **桌面客户端**（预览） —— 同一份 Python 核心上的一个窗口：选择目录、在应用里添加提供商的 key、运行任务并实时查看输出与审批、每个任务独立的笔记、HTML 产物预览。界面支持中文和英文。见[桌面客户端](#桌面客户端预览)。
- **搜索增强** —— 搜索和网页阅读在模型服务商的服务器上完成（DeepSeek；选用 MiniMax 时是 MiniMax）。你的机器只需要连上模型 API，在受限网络下也能搜索。`web_research` 把多个查询并行发出。Bing 和 DuckDuckGo 是免 key 的后备。
- **Docker 模式** —— 无人值守的工作在沙箱里完成。`goal` 在你仓库的 git-worktree 副本上运行：Docker 沙箱内、默认离线、有预算上限，最后交给你一个待审阅的 git 分支。供其他 agent 调用的 `exec`，shell 命令默认在沙箱内执行。在 chat 里，`/sandbox` 随时开启沙箱。
- **会话间 Agent 沟通** —— 一个任务里的 Rocky 可以把工作交接给一个新任务，也可以给另一个任务的收件箱留言（`/task`）。留言作为参考信息留在收件箱里，等那个任务来读；标题下方的 ✉ 显示有几条在等。

我们也提供了一批仍在打磨的**实验性功能**，探索 harness 的自进化 —— 以及如何用 harness 产生的使用轨迹做更好的后训练，最终让模型与框架协同增强。

**同一个引擎，多个入口：**

| 入口 | 命令 | 作用 |
|---|---|---|
| **终端（Terminal）** | `rockycode` | 终端界面。Rocky 通过原生工具调用读代码、改代码、跑代码，并实时流式展示推理过程。 |
| **桌面客户端**（预览） | `apps/desktop`，从源码运行 | 同一份 Python 核心上的一个窗口：选择目录、运行任务并实时查看输出与审批、每个任务独立的笔记、HTML 产物预览，以及模型竞技场。目前从源码运行。 |
| **沙箱保护的自主运行** | `rockycode goal` | 无人值守地执行一个目标 —— 在你仓库的独立 git-worktree **副本**上、Docker 沙箱内、硬性预算上限之下，驱动「规划 → 验证 → 复审」循环。产出是一个供你审阅的 git 分支。 |
| **Harness benchmark** | `rockycode bench` | 把同一套智能体循环放到 SWE-bench Verified 上，用官方 harness 打分。提示词或循环逻辑的每次改动，都可以被测量。 |

此外还有沙箱保护的 **`exec`**：rockycode 可被其他 agent 调用并分配任务，用于
自动化流程；同样在 Docker 沙箱内运行，为无人值守的执行增加一层安全保护。

每次会话 —— 无论交互还是基准 —— 都被记录为可直接用于训练的轨迹
（trajectory）。因此这个 harness 同时也是一个**强化学习环境**：为后续微调
小模型（工具调用、上下文压缩、记忆角色）打下基础。

## 能力量化：SWE-bench

完整 500 题的成绩：**独立的完整 500 题运行**，每个模型各 3 轮，所有模型使用完全相同的 harness 与配置（步数上限 100、单次最大输出 32,768 token、推理力度 `max`、thinking 开启），由官方 SWE-bench harness 打分。全部 500 题用的是同一份提示词、同一套循环。注意版本差异：`deepseek-v4-flash` 一列是**正式版（GA，V4-Flash-0731）**；而 `deepseek-v4-pro` 的三轮在 GA 版 V4-Pro-0813 发布之前运行，该列是 **preview 版**成绩 —— flash 与 pro 的差距里含有版本差。

| 轮次 | `deepseek-v4-flash`（GA） | `deepseek-v4-pro`（preview） | `minimax-m3` |
|---|---|---|---|
| 第 1 轮 | 90.0%（450/500） | 75.6%（378/500） | 72.8%（364/500） |
| 第 2 轮 | 88.6%（443/500） | 74.8%（374/500） | 71.2%（356/500） |
| 第 3 轮 | 87.8%（439/500） | 74.4%（372/500） | 70.0%（350/500） |
| **平均** | **88.8%** | **74.9%** | **71.3%** |
| 多轮并集（pass@3） | 95.4%（477/500） | 81.8%（409/500） | 83.6%（418/500） |

两行汇总要分开读。**平均值**是可以与排行榜对比的数字 —— 每一轮都是独立的单次完整 500 题。**并集**是 pass@3：至少被某一轮解出的任务。两者之间的差距（DeepSeek 两个模型各约 7 个点、MiniMax 约 12 个点）是轮次间方差：模型在这套 harness 下已经"够得着"这些任务，在其中一部分轮次里稳住了。当前的工作重心就是收掉这个差距，靠 finish 前的验证门控与多轮选择。作为参照：DeepSeek 用自家 scaffold 报告 V4 Pro (Preview) 为 80.6%。逐轮拆解请关注我们的X账号（@rockycode_ai）。

另有两轮较早的 flash-preview 运行（79.8% —— 即此前列在这里的那一轮 —— 与 81.2%）在运行途中遭遇本地网络中断，表现为连续任务块返回空补丁；这两轮已用干净的重跑替换。

**接下来的基准：** DeepSWE 正在接入；之后会支持更多长程任务（long-horizon）。

## 快速开始

环境要求：Python 3.11+（经 [uv](https://docs.astral.sh/uv/) 管理）和一个模型提供商的 API key。首次运行会问你的模型从哪里来：目录里的任意提供商（默认是 DeepSeek）、免 key 的本地 Ollama，或你自己的 OpenAI 兼容 URL。对 chat 及 research/learn 模式而言，这就是全部安装。

**Docker Desktop** 仅在需要容器隔离工具执行的模式下才必需：`goal`（自主运行）、`exec`（自动化委托）、`bench`（SWE-bench 打分），以及 chat 里可选的`/sandbox`。这些模式在沙箱内默认离线运行，被委托或无人值守的任务因此留在沙箱之内，与你的主机和网络隔开。

```bash
uv tool install rockycode     # 推荐 —— `rockycode` 命令直接进 PATH
rockycode                     # 首次运行会引导你完成 API key 设置
```

已经装过、想升到新版本？用 **`uv tool upgrade rockycode`** ——
升级要用 `upgrade`：重复执行 `uv tool install` 时，它看到已有安装就保留旧版本。

第一次用 uv？一条命令安装：
`curl -LsSf https://astral.sh/uv/install.sh | sh`（Windows 及其他方式见
[uv 安装文档](https://docs.astral.sh/uv/getting-started/installation/)）。

三种装法看着像，落点完全不同：

- **`uv tool install rockycode`**（推荐）—— 给 CLI 一个独立的隔离环境，并把
  `rockycode` 命令放上 PATH；系统 Python 低于 3.11 时，uv 会自动拉取一个匹配
  的解释器。想「当应用装」就用它。
- **`uv pip install rockycode`** —— 只装进**当前激活的虚拟环境**：`rockycode`
  命令只存在于那个 venv 里，新开的 shell 要先激活 venv 才能找到它（或用 `uv run rockycode` 运行）。
- **`pip install rockycode`** —— 同样只进当前环境，且要求 Python 3.11+。在更老
  的 Python 上会报一个很有误导性的
  `ERROR: No matching distribution found for rockycode`。原因：pip 只会提供
  `requires-python` 与你解释器匹配的版本，老 Python 下可装的版本数是零，于是 pip 报成「找不到包」。遇到这个错，直接用上面的 `uv tool install rockycode`，uv 自带 3.11+ 的 Python。

或从源码安装：

```bash
git clone https://github.com/cicialgo/rockycode.git && cd rockycode
uv tool install .
```

首次启动只需粘贴一次 API key。它被存入操作系统钥匙串（macOS 和 Windows 已内置；Linux 需安装 `[keyring]` 扩展），或者在你选择那一行时，存入 `~/.rockycode/.env` 私有文件（权限 `0600`）。两个位置都在你的项目和 shell 配置之外。rockycode 只从它自己的存储里读 key。项目内的 `.env` 会被忽略，所以打开一个克隆来的仓库时，key 和 endpoint 仍然由你掌握；其中形似凭据的变量只按名字给出警告，值保持未读。存储里的内容只留在 rocky 自己的进程里：rocky 替模型运行的命令拿到的只有你的 shell 环境。如果你自己的命令需要某个变量，请在 shell 里 export。

在任意项目里运行：

```bash
rockycode                        # 当前目录
rockycode -C ~/code/myproject    # 其他任意项目
rockycode -r                     # 浏览历史会话，挑一个恢复（也可 -r <id> 直接恢复某次）
```

要开发 rockycode 本身？`uv sync && uv run rockycode` 直接从克隆目录运行。

### 终端设置

TUI 完全支持鼠标：滚轮翻历史、点击文件链接、把文档停靠在对话旁，以及
拖选任意文本即复制（松开即复制，有 toast 确认）。

| 终端 | 设置 |
|---|---|
| **ghostty** | 开箱即用：鼠标和剪贴板都已就绪。 |
| **iTerm2** | 打开 **Settings → Profiles → Terminal → "Enable mouse reporting"**，滚动、点击、拖选才能到达应用。按住 `⌥` 拖动可保留 iTerm2 原生选区。剪贴板开箱即用（复制走 `pbcopy`）。 |
| **VS Code 终端** | 开箱即用：鼠标事件默认开启。 |

SSH 远程会话下剪贴板走 OSC 52 —— 在本地端开启「允许应用访问剪贴板」
（或你终端里的对应选项）。

## 交互使用

### 斜杠命令

| 命令 | 说明 |
|---|---|
| `/help` | 列出所有命令 |
| `/plan` | 在当前对话里进入交互式规划：一起讨论、修订，审阅完整计划，批准后在这里执行或交给 `goal` |
| `/goal [目标]` | 进入自主模式的独立界面（需要 Docker） |
| `/task` | 关联任务：`new <交接说明>` · `open <id>` · `list` · `send <id> <消息>` · `inbox` · `use <消息 id>` · `dismiss <消息 id>`。项目里有两个以上任务时，它们以可点击的音符排在标题下方：`♪ 01 修登录   ♫ 02 测试 ✉1`，点一下即切换（或输入编号：`/task open 2`），`✉` 表示有待读消息 |
| `/loop 5m <prompt>` | 在本次对话中每 5 分钟检查一次。只输入 `/loop` 列出全部；`stop` · `pause` · `allow` |
| `/research` | 研究模式：deep-research · paper-reading · whiteboard · prove |
| `/learn` | 导师模式：目标是你的理解 |
| `/model` | 切换提供商与模型（见下） |
| `/effort off\|low\|high\|max` | 推理深度，会话内实时可调（按位置收敛到各提供商自己的档位） |
| `/permission yolo\|ask\|careful` | 本次会话的工具审批严格度 —— 只输入 `/permission` 打开选择器；也可按 `shift+tab` 循环切换，或点击状态栏的 🔒 标记 |
| `/sandbox on\|off\|status` | 把工具执行隔离进容器 |
| `/lsp` | 语言服务器桥接（实验性）：状态与设置方法。lint 和类型检查由 `check_code` 独立完成 |
| `/artifact` | 作品面板：可搜索的选择器，覆盖整个项目或本次会话，可预览、修改、查看版本历史。每个 HTML artifact 有固定 ID 和带编号的版本；`import <文件>` 把以前的 HTML 文件收进来 |
| `/paste` | 粘贴剪贴板图片（或 `ctrl+v`）；纯文本模型自选识图路由 |
| `/prompt` | 查看当前生效的系统提示词 |
| `/mcp` | 已连接的 MCP 服务及其工具 |
| `/skills` | 已安装的技能 |
| `/memory` · `/remember <内容>` | 查看记忆 · 存一条笔记 |
| `/proposals` · `/routines` | 审阅 dream 起草的技能 · 运行或授权（lease）周期性例程（`evolve = auto` 或有待处理项时才列出） |
| `/config [键] [值]` | 查看或设置偏好 |
| `/queue [on\|off\|resume\|clear]` | Rocky 工作中发来的消息：打断（`off`，默认）或等当前轮结束后执行（`on`）；查看、恢复或清空排队消息 |
| `/parked` | 只有你能批准、`yolo` 为你留下的步骤，在这里决定（有步骤在等时才列出） |
| `/clear` · `/exit` | 会话控制 |
| `! <命令>` | 直接执行 shell 命令；输出进入 Rocky 的上下文 |

**偏好设置。** `/config ui_language zh` 把界面切换为中文（`en` 为英文；`auto` 跟随 `language`）。`/config language` 设置 Rocky 回答用的语言（`auto` 跟随你输入的语言）。`/config currency cny` 按各提供商自己的人民币价格表显示花费，`rockycode pricing` 打印当前生效的价格表。

### 模式

- **规划模式**（`/plan`）—— 会话变为只读，唯一可写的是一份计划文件。Rocky 探索代码库、起草计划。你批准之后才开始动手：当场执行，或交给 goal 模式。
- **研究模式**（`/research`）—— 一组可选的提示词契约：**deep-research**
  （多来源、经事实核查的报告）、**paper-reading**（读论文）、**whiteboard**
  （一起在白板前想问题），以及 **prove** —— 通过内置的 `lean-prover` 技能
  （Mathlib 与 TorchLean），把一个非形式化的数学命题变成经 Lean 4 编译器
  认证的结论。*（prove 为[实验性功能](#实验性功能)。）*
- **学习模式**（`/learn`）—— 初学者模式：给解释、查理解。

### 模型与提供商

DeepSeek 是主场模型，而**每个模型都是数据**：整份目录都在
`rockycode/models.toml` 里 —— 每个提供商一个国内 base URL、一个 key 名、
一种推理参数形状；每个模型有自己的上下文窗口、最大输出、视觉标记、价格与
角色。所有模型专属的数字，引擎都从这份 spec 里读，所以新增一个模型只是改数据。你自己的 `~/.rockycode/models.toml`（同样形状）会深度合并在上面：加
模型、改上限、补价格、隐藏某一行。

| 提供商 | 模型（❖ = 支持图片输入） | 上下文 / 最大输出 |
|---|---|---|
| **deepseek**（默认） | `deepseek-flash`（V4.1 Flash，默认）❖、`deepseek-v4-pro` | 1M / 384K |
| **glm** | `glm-5.3`、`glm-5.3-flash` ❖ | 1M / 128K |
| **kimi** | `kimi-k3` ❖、`kimi-k3.1-pro` ❖ | 1M / 128K |
| **minimax** | `minimax-m3` ❖、`minimax-m3.1-flash` ❖ | 1M / 128K |
| **stepfun** | `step-5-preview` ❖ | 1M / 64K |
| **qwen** | `qwen3.8-max` ❖、`qwen3.8-flash` ❖ | 1M / 64K |
| **qwen-plan** | `qwen4-max` ❖ | 1M / 128K |
| **mimo** | `mimo-v2.6-pro` ❖ | 1M / 128K |
| **openrouter**（免费，需手动选用） | `openrouter-free` ❖（Free Models Router） | 64K / 8K |
| **ollama**（本地，$0） | 你拉取过什么就是什么 —— 从运行中的服务实时发现 | 以服务端实测为准 |

OpenRouter 是免费路径，一个免费 key 就能开始，协议为 OpenAI 兼容的
`https://openrouter.ai/api/v1`。用 `/model` →「添加或更换 key」添加
`ROCKYCODE_OPENROUTER_API_KEY`（到 https://openrouter.ai/keys 创建）。这是
rocky 自己的变量名；校验器使用的 `OPENROUTER_API_KEY` 是另一个 key，只给校验器用。启动默认仍是 DeepSeek。`/model openrouter` 发送
`openrouter/free`（在支持本次请求的免费模型里挑选，包括工具调用；路由器
可能落到很小的模型上）。免费档限额（OpenRouter 文档，2026-10-23 核对）：累计购买不足 $10 额度时
每分钟 20 次、每天 50 次；达到至少 $10 后每天 1,000 次。价格
留空。发往 OpenRouter 的请求省去推理参数（`reasoning = none`）。rocky 把上下文夹紧到当天最小的、支持工具调用的 `:free` 窗口（64K / 8K）；路由器对外公布的上限是 200K。Groq（`https://api.groq.com/openai/v1`）和 Gemini 的
OpenAI 兼容端点（`https://generativelanguage.googleapis.com/v1beta/openai/`）
由你自己添加到 `~/.rockycode/providers.toml`。

每个提供商只有一个国内端点（key 名 `ROCKYCODE_<提供商>_API_KEY`；旧的
`_CN_`/`_EN_` 名字仍然能读），上面的 OpenRouter 除外。自带 URL 与 key 的订阅套餐单独成行 ——
`qwen-plan`（百炼 Token Plan）、`mimo-plan`、`stepfun-plan` —— key 名为
`ROCKYCODE_<提供商>_PLAN_API_KEY`。`/model` 选择器**先选模型**（每个模型
一行）；选中的模型如果有多个端点，再问由哪个 URL 提供服务（官方 · 套餐 ·
你自己的），其中「custom base URL」一行可以填你自己的网关或代理，按提供商
记住（`~/.rockycode/endpoints.toml`，以 `<提供商>-custom` 寻址）。直接输入
spec 可以跳过这一切：`/model glm:flash`、`/model qwen-plan:qwen3.8-max`。已
下线的 `deepseek-v4-flash` / `-vision-exp` 名字仍可解析（落到
`deepseek-flash`，与 DeepSeek 自己的路由一致）。选择器只展示已配置好 key
的提供商。

#### 各提供商的 key 名与主机

内置的**模型提供商**使用 Rocky 自己的 key 变量名。添加或更换 key 在 Rocky 里完成：
终端里用 `/model` →「添加或更换 key」，桌面客户端里用 key 表单。Rocky 用一次免费调用
测试这个 key，保存它，并立即使用。有操作系统钥匙串时 key 存进钥匙串；
你选择 key 文件那一行时，存进 `~/.rockycode/.env`（一行 `NAME=value`，权限 `0600`）。
同一个列表会显示每个 key 存在哪里，也可以把还留在文件里的 key 移进钥匙串。
shell 里用同一个名字 export 的 key 优先。项目目录的 `.env` 和环境里的
`OPENAI_API_KEY` 会被忽略。校验器有单独的读取规则，见下文。key 请留在聊天之外：配置工具会拒绝形似密钥的参数，密钥应该填进 key 输入框。如果你手动修改
`~/.rockycode/.env`，重启 Rocky 才会加载新变量。每个模型使用它自己提供商的 key，
有一个提供商的 key 就可以开始。

下表列的是 **1.0.0 内置默认值**；厂商可能还提供更多服务。多数云提供商默认使用
国内主机，OpenRouter 是国际入口，Ollama 是本地服务。国际账号应使用对应服务的
国际 URL：通过选择器的「custom base URL」或 `~/.rockycode/endpoints.toml` 添加，
再明确选择 `<提供商>-custom:<模型>`。自定义 URL 沿用该提供商的主 key。裸模型 id 仍走它原来的路由，模型 id 按你输入的原样发送。

| 提供商 | key 名 | 内置 base URL | 内置套餐行 · 其 key | 国际 base URL | 控制台 |
|---|---|---|---|---|---|
| **deepseek** | `ROCKYCODE_API_KEY` | `https://api.deepseek.com/v1` | — | 全球同一主机 | platform.deepseek.com |
| **glm** | `ROCKYCODE_GLM_API_KEY` | `https://open.bigmodel.cn/api/paas/v4` | — | `https://api.z.ai/api/paas/v4` | open.bigmodel.cn · z.ai |
| **kimi** | `ROCKYCODE_KIMI_API_KEY` | `https://api.moonshot.cn/v1` | Kimi Code 会员是另一套服务，见下文 | `https://api.moonshot.ai/v1` | platform.moonshot.cn · platform.moonshot.ai |
| **minimax** | `ROCKYCODE_MINIMAX_API_KEY` | `https://api.minimax.cn/v1` | — | `https://api.minimax.io/v1` | platform.minimax.cn · platform.minimax.io |
| **stepfun** | `ROCKYCODE_STEPFUN_API_KEY` | `https://api.stepfun.com/v1` | `stepfun-plan` · `ROCKYCODE_STEPFUN_PLAN_API_KEY` | — | platform.stepfun.com |
| **qwen** | `ROCKYCODE_QWEN_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plan`（百炼 Token Plan）· `ROCKYCODE_QWEN_PLAN_API_KEY` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | bailian.console.aliyun.com |
| **mimo** | `ROCKYCODE_MIMO_API_KEY` | `https://api.xiaomimimo.com/v1` | `mimo-plan` · `ROCKYCODE_MIMO_PLAN_API_KEY` | — | 小米 MiMo 开放平台 |
| **openrouter** | `ROCKYCODE_OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | — | 同一主机 | openrouter.ai/keys |
| **ollama** | 免 key | `http://localhost:11434/v1` | — | — | — |

内置套餐 URL 分别是 `https://api.stepfun.com/step_plan/v1`（`stepfun-plan`）、
`https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`（`qwen-plan`）和
`https://token-plan-cn.xiaomimimo.com/v1`（`mimo-plan`）。破折号表示内置行只到普通行为止；厂商可能仍有订阅产品。注册表仍将 StepFun 套餐 URL 标为「首次调用时验证」：本次发布的 canary 跳过了它。

**MiMo Token Plan 的区域：**套餐 key 绑定在签发它的集群上。内置的 `mimo-plan`
行是中国区；新加坡区是 `https://token-plan-sgp.xiaomimimo.com/v1`，欧洲区是
`https://token-plan-ams.xiaomimimo.com/v1`，用错集群会返回 `401 Invalid API Key`
（套餐 key 以 `tp-` 开头，团队套餐 `ttp-`；`sk-` 开头的是按量付费 key，应放在
`mimo` 行）。新加坡或欧洲的 key，在 `~/.rockycode/models.toml` 里把套餐行指向
自己的集群（与内置注册表深度合并，保留已有的其他项），然后
`/model mimo-plan:mimo-v2.6-pro`：

```toml
[providers.mimo.plan]
base_url = "https://token-plan-sgp.xiaomimimo.com/v1"
```

1.0.0 的 canary 就是这样实测了新加坡集群；中国区和欧洲区的集群还在等一次实测。

**MiniMax 国际版：**将国际版 key 存在 Rocky 的 `.env` 中，变量名为
`ROCKYCODE_MINIMAX_API_KEY`，然后在 `~/.rockycode/endpoints.toml` 添加或更新这一项
（保留已有的其他项）：

```toml
minimax = "https://api.minimax.io/v1"
```

再选择 `/model minimax-custom:minimax-m3.1-flash`（发送的模型 id 为
`MiniMax-M3.1-Flash`）或 `/model minimax-custom:minimax-m3`。主连接的 `ROCKYCODE_BASE_URL` 请保持原样：改它会连同主连接的 key 一起改道。

**Qwen Token Plan：**将套餐 key 存为 `ROCKYCODE_QWEN_PLAN_API_KEY`，选择
`/model qwen-plan:qwen3.8-max` 或 `/model qwen-plan:qwen3.8-flash`，端点已经内置。
裸 id `qwen3.8-max` 选择的是普通 `qwen` 行，用普通 key；要用套餐 key，请写出套餐行。Token Plan 有自己的 URL，与上表普通服务的国际 URL 是两个地址；以账号控制台提供的服务与 URL 为准。

Qwen4 Max 通过 Token Plan 提供：用 `/model qwen-plan:qwen4-max` 切换，或用
`rockycode chat --model qwen-plan:qwen4-max` 启动。它取代了 Qwen4 Max Preview，
已保存的 `qwen4-max-preview` 会解析到 Qwen4 Max，端点和 key 相同。推理档位是
`low`、`high`、`xhigh`，输出上限是 131,072 token。

Kimi Code 会员（编程套餐）是开放平台之外的另一套服务：主机和模型 id 都是它自己的。在
`~/.rockycode/providers.toml` 给它单独一行，然后 `/model kimi-code:k3`：

```toml
[kimi-code]
label = "Kimi Code plan"
base_url = "https://api.kimi.com/coding/v1"   # 国际版：https://api.kimi.ai/coding/v1
key_env = "ROCKYCODE_KIMI_CODE_API_KEY"
models = ["kimi-for-coding", "k3", "k3-256k"]
reasoning = "effort"
thinking_off = false
vision = true
```

将会员 key 存为 Rocky `.env` 中的 `ROCKYCODE_KIMI_CODE_API_KEY`。如果以前将会员
key 放在 `ROCKYCODE_KIMI_API_KEY`，把它移到新变量名：两个变量名各自独立。`ROCKYCODE_KIMI_API_KEY` 留给开放平台 key。1.0.0 的 `kimi-code` 是用户自行添加的行。`key_env` 就是你存 key 时用的变量名，只要这一行
和 `.env` 里一致即可。canary 实测了国内 coding URL 上的 `kimi-for-coding`、`k3`
和 `k3-256k`，国际 coding URL 也接受同一个 key；视觉能力还在等一次真实验证。

**OpenRouter 校验器：**Jev 有它自己的 key，与上表的模型提供商 key 分开。使用 OpenRouter
校验器时，在 Rocky 的 `.env` 中将其 key 存为 `ROCKYCODE_VERIFIER_API_KEY`
（或导出至 shell）。读取顺序是 `ROCKYCODE_VERIFIER_API_KEY`、旧变量名
`ROCKYCODE_JEV_API_KEY`、`OPENROUTER_API_KEY`；最后一个名字仅对应 OpenRouter
校验器。切换服务前清理失效的高优先级覆盖值。后续 Jev 实测在进程中将模型 key 名
映射到了 `OPENROUTER_API_KEY`，在 1.0.0 里这一步由你手动完成。

若要启用 OpenRouter 校验器，将以下表合并到 `~/.rockycode/config.toml`
（已有 `[verifier]` 时，修改那一张表），然后重启：

```toml
[verifier]
mode = "shadow"
provider = "openrouter"
timeout_ms = 5000
```

shadow 模式会将脱敏后的审批上下文发送给该服务仅用于观察；工具的批准仍由你决定。
shell 或 Rocky `.env` 中的校验器设置优先于这张表。默认发送的模型 id 为
`typesafe/jev-1.13`，端点为 `https://openrouter.ai/api/v1/systemone`
（与编程模型的 chat 端点是两个地址）。

排查提示（同一个 HTTP 状态码可能有多种原因）：

- 国内主机回 `401` → 检查 key 的签发控制台、有效性及所属服务。如果是国际版 key，
  配置对应服务的国际 URL 并明确选择 `-custom` 行；401 也可能来自 key 本身或所属服务，请先检查这两项。
- 拿订阅套餐的 key 打普通行回 `401` → 检查订阅状态与 key 变量名，再选择对应的套餐
  行（`qwen-plan:qwen3.8-max`）或上面的 `kimi-code`。
- Rocky 停下来并给出一个 key 变量名 → 用 `/model` →「添加或更换 key」添加该提供商
  的 key，或选一个已有 key 的行。
- 校验器报 `missing_api_key` → 使用上面的覆盖变量，或所选校验器对应的
  `TYPESAFE_API_KEY`、`OPENROUTER_API_KEY`、`PERPLEXITY_API_KEY`。
- 在聊天里请 Rocky 以 `action="show"` 调用 `rocky_config`，会列出所有端点及
  「key set (NAME)」「key NOT set — NAME」或「no key needed」。这是聊天里的一次工具调用。`/model` 使用更短的本地化标签，通常只显示已配置的提供商。

以上是 1.0.0 已有能力的手工配置方法。`rockycode doctor`、`/doctor` 及新增的
`rockycode key`、`url`、`provider` 命令均为 **1.1 规划**。

视觉能力按「模型」区分：`deepseek-flash` 用主 key 就能识图，默认会话贴图即
用。纯文本模型（`deepseek-v4-pro`、`glm-5.3`）收到的贴图会由
`deepseek-flash` 静默描述（配置 `image = auto`），或交给你自己的 CLI（`image = mmx`）。
`rockycode config model <spec>` 把任意选择固化为启动默认。上下文窗口与最大
输出跟随当前模型（配置 `context_window` / `max_tokens` = `0`）；填一个数字
则是你自己的上限，切换模型也不变。DeepSeek 与 MiniMax 都有完整 500 题的
bench 成绩（见上方「能力量化」）；其余提供商属于[实验性功能](#实验性功能)。

推理深度旋钮（`/effort off|low|high|max`）与提供商无关；各提供商自己的档位
来自注册表，旋钮在请求层按位置收敛到它们上面（StepFun 的
`low|medium|high` 里 rocky 的 `high` 对应 `medium`；GLM 与 Kimi 始终开启 thinking，`off` 会发它们的最低档）。`xhigh` 仍然接受，等于 `max`。

rocky 也能配置它自己：告诉它「用我的代理」「加上我的 vLLM 服务」「换默认
模型」，内置的 `rocky-setup` 技能加上需要确认的 `rocky_config` 工具就会在
`~/.rockycode` 下完成修改。key 始终留在你手里：它告诉你变量名，值由你自己粘贴。

#### 本地模型（Ollama）

rocky 集成的是 OpenAI 兼容*协议*，运行时推荐 [Ollama](https://ollama.com)（0.19 起在 Apple Silicon 上用 MLX 引擎）。两步就绪：`ollama serve` 跑起来，拉一个支持工具调用的模型
（推荐并实测过 `ollama pull qwen3.8:27b-mlx`），它就会出现在 `/model`
里 —— 列表是实时发现的、按 `$0 · local` 计价。

切到本地模型前会先跑一遍**就绪预检** —— 服务已启动、模型已拉取、支持工具调用、serving 上下文足够 —— 有问题就拒绝切换，并给出确切的修复命令
（`ollama pull …`、`export OLLAMA_CONTEXT_LENGTH=65536`）。最要紧的一条：
Ollama 默认上下文很小且**静默截断**，agent 会话会以难以排查的方式中断 ——
请用 `OLLAMA_CONTEXT_LENGTH=65536` 启动。每次切换 rocky 都会把自己的
`context_window` 对齐到服务端已验证的值。

其它本地服务（LM Studio、llama.cpp、vLLM）同样只是数据：在
`~/.rockycode/providers.toml` 里给提供商加一行 `local = true`，
它的端点就免 key。

## 自主运行

### Goal 模式

`rockycode goal "<目标>"` 让 Rocky 无人值守地工作，最终交给你一个待审阅的
git 分支。安全是结构性的：

- 运行发生在你仓库的 git-worktree **副本**上 —— 你的工作区保持原样。
- 工具执行被限制在 Docker 沙箱内，默认离线。
- 每条 bash 命令都先过分类器：毁灭性命令（对根目录 `rm -rf`、`mkfs` 等）
  直接拒绝；有风险但正当的命令（`git push`、`sudo`、安装依赖）需要一次
  预先批准。
- **预算上限** —— 花费、墙钟时间、token，按每个模型自己公布的价格表计算
  （含 DeepSeek 的高峰期加价）—— 会优雅地终止运行，且最坏情况的花费在运行
  *开始前*就打印出来。

循环把目标规划成里程碑，用你项目自己的 linter（`check_code`）逐一验证，
并由周期性复审者重新规划以保持方向。从小而便宜的目标开始：

```bash
rockycode goal "给 <fn> 加一段 docstring 并跑 linter" --max-usd 0.50 --max-hours 1
```

### 自动委托：`exec`

`rockycode exec "<任务>"` 是单次、非交互的入口，专为被*其他*智能体和脚本
调用而设计。stdout 是 JSONL：一行 `meta`、模型的 `text`、以及带证据（改过
的文件、跑过的命令、被拒的操作）的 `result` 信封，交给调用方自己核验；`--events` 会补上逐工具的回执行。预算始终强制生效；退出码
区分成功、失败、待审批、预算终止。退出码 2 的 `result.blocked_on.grant`
给出下一次调用可显式授权的 token。

用 `--profile` 决定 rocky 能做多少：`read`（read_file / grep / glob /
view_image，仅此四个只读工具）和 `write`（多了限制在 `--workdir` 内的 write_file / edit_file）直接在主机上运行，秒开 —— 这正是
Claude Code 或 Codex 想要的「看看这个仓库告诉我」或小改动委托，用便宜又快
的模型跑。`full` 加上 bash，**默认**在 Docker 沙箱内（沙箱才是边界；命令分类器是纵深防御）。

```bash
rockycode exec --profile read "重试逻辑在哪个模块里，被哪些地方调用？"
rockycode exec --profile write "给 utils.py 里每个公开函数补上 docstring"
```

检查退出码 2 的执行回执后，用会话 ID、授权和**明确的后续任务**继续（替换示例 ID）：

```bash
rockycode exec --resume rk_ab12cd34 --allow pkg-install \
  "前面的文件修改已经完成。安装被阻塞的依赖，再运行测试，保留已完成的修改。"
```

`--resume` 与 chat 一样，把消息和工具结果带入一个**新会话**，然后执行后续任务。
它只执行后续任务：是否重试被阻塞的工具、是否重做原始任务，都由你的后续任务来决定。后续任务是必填项；要从 stdin 读取，请显式传 `-`。`meta.resume` 会标明
`mode: "history-only"`、`requires_prompt: true` 和 `from_session`；返回的是新会话 ID。

**resume 可能重复已做过的工作**：模型可能发起新的工具调用，崩溃或中断的工具也可能已经产生部分副作用。重试或从头重跑原任务之前，先核验已有结果；旧日志中的 `[error] tool execution was interrupted` 尤其要当作“可能已经执行”。Docker 会重新创建，容器内的临时状态从头开始。

`--allow` 可重复，例如 `--allow pkg-install --allow delete`，按类别授权且只对本次调用生效；每次新的调用各自声明授权，无条件禁止的操作始终禁止。profile、model、
sandbox/network 和 `--allow-dir` 同样取本次启动设置。会话跟随项目注册的当前目录；
显式 `--workdir` 与它冲突时，会在启动工具或 Docker 前报错。新 exec 会话会记录
项目身份，项目移动并重新注册后仍可查找。

### Codex MCP：`mcp`

`rockycode mcp` 是一个 stdio 上的 Model Context Protocol 服务，让 Codex
（ChatGPT 桌面里的 Codex 应用，以及 Codex CLI）把 rocky 当工具来调用。`rockycode serve` 是另一个入口：它说的是编辑器用的 rocky 自己的 JSON-RPC。工具名是 `rocky_exec`，默认 profile 为 `read`（只有调用方显式传入 `write` / `full` 时才放宽）。在 MCP 客户端中将 `rockycode mcp` 注册为 stdio 服务，
可参考随仓库提供的[配置示例](integrations/codex-plugin/mcp.json)。默认工具超时
为 60 秒；调用会消耗模型 API 额度，并可能将任务上下文和文件内容发送给配置的模型服务商。

### 编辑器集成：`serve` 与 VS Code 扩展

`rockycode serve` 把引擎以 JSON-RPC 2.0（经 stdio）暴露出来，保持
UI 无关。仓库自带的 VS Code 扩展（`rockycode-vscode/`）构建其上：侧边栏
对话、流式推理展示、内联工具审批卡片、diff 预览 —— API key 保存在
VS Code 加密的 Secret Storage 里。

### 桌面客户端（预览）

`apps/desktop/` 是终端同一份 Python 核心上的一个窗口。智能体循环留在 Python 核心里，所以桌面和终端共用一个引擎、同一批模型、同一套审批策略。

- **目录里的任务** —— 通过系统选择器选定工作目录。每个任务有自己的草稿、笔记和模型。
- **应用内的 key 表单** —— 把提供商的 key 粘贴进隐藏输入框。应用用一次免费请求测试它，然后存入系统钥匙串（有钥匙串时），或 0600 权限的 key 文件。
- **审批** —— 与终端相同的策略，按任务设置：`ask`、`careful`、`yolo`。只有你能批准的步骤会留在一个列表里等你。
- **HTML 预览** —— HTML 产物在右侧的沙箱面板里打开。
- **Model Race（田忌赛马）** —— 五个已配置的模型、同一个任务、五次互相隔离的运行，你来打分，还可以加一个匿名的模型裁判。导出前先显示完整的本地 JSON 预览，文件留在你的机器上。
- **界面语言与主题** —— 中文或英文；**System** 主题跟随系统，**Nebula** 是终端的紫色夜空配色。
- **更新提醒** —— 有新版本时出现一条横幅。下载和安装由你决定。

目前从源码运行（Electron + React，基于 Python 核心），安装包会在之后的版本提供。安装只需几条命令：[apps/desktop/README.zh-CN.md](apps/desktop/README.zh-CN.md)。

## 记忆（实验性功能）

Rocky 跨会话记忆，载体是 `.rockycode/memory/` 下的纯 markdown 文件
（事实 / 技能 / 经历 / 反馈）—— 文件即真相，可随意编辑。`MEMORY.md` 和用户
反馈进入每次会话；其余的只留一行索引，按需经 `recall_memory` 工具取回 ——
既可按名字，也可按语义。语义检索跑在本地 Ollama 向量上（英文
`nomic-embed-text`，中文及跨语言 `qwen3-embedding:0.6b`），底层是会自动重建的
sqlite-vec + FTS5 索引；Ollama 缺席时平滑退回关键词检索。删除是把文件移入归档。命令行检视：`rockycode memory list|show|search|reindex|edit|rm`；
`--no-memory` 关闭。

> ⚠️ 我们在测试中发现，开启 `/memory` 会让「记忆」主导当前项目下的每一次会话 ——
> 它可能把一个本来正常的任务，硬按旧记忆的模式扭曲完成（so sad）。因此目前建议
> 只用于实验。

## 实验性功能

以下功能已经可用，但仍属早期 —— 需主动开启，接口可能还会变。任何可能自行
动作的东西都**默认关闭**。

- **Dream** *（早期，测试有限）。* `rockycode dream` 在你休息时整理近期会话：
  一个本地 Ollama 模型（默认 `qwen3.5:2b`，零 API token）把每条轨迹消化成经历
  笔记，将新事实与旧记忆对账（矛盾的被归档并保留），重写 `MEMORY.md` 中由
  dream 维护的状态段，并重建索引。`--dry-run` 预览每一步。仍处于早期，目前建议只用于实验。
- **自我改进** *（默认关闭）。* 在整理之上，dream 会把每次会话评定为结果记录、
  把反复出现的失败挖掘成弱点笔记，并把候选技能 —— 以及从你反复手动做的事里
  提炼出的候选**例程** —— 起草进提案收件箱。每一份草稿都等你来决定：你在 `/proposals` 里批准或归档；批准后的**例程**（`/routines`）预先授权、带预算，
  以有期限的 lease 运行，到期退回「点击才跑」。1.0.0 起默认关闭，因为每次反思都会
  消耗你的模型额度：`/config evolve auto` 开启（`evolve = off` 关闭全部调用）。
- **形式化证明** —— `/research prove` 与内置的 `lean-prover` 技能，把一句非
  形式的数学或模型断言，变成 Lean 4 判定（绿·已编译 / 绿·内核复查
  `lake check`，需 Lean v4.35+ 与 Linux bubblewrap / 绿·paranoid / 琥珀 / 红 /
  有争议），基于 Mathlib 与 TorchLean。工具链有 `lake check` 时，只有通过内核复查的结果才叫「证明了」。已测试但仍在打磨；开启它需要下载超大的 Lean 4 工具链安装包。
- **`explore` —— 只读委派。** chat 可以向一个全新上下文的子进程「购买」一次
  有界的只读调查，只拿回带引用、经机械校验的报告；搜索噪声留在你的会话之外。
  它同样为 goal 模式的分支评审与里程碑验证提供依据。
- **各模型的 bench 成绩。** GLM、Kimi、MiniMax、StepFun、Qwen、MiMo 都是
  注册表里的条目，在 `/model` 里选用。DeepSeek 与 MiniMax 已有完整 bench
  成绩（见「能力量化」）；其余条目还在等自己的成绩。`models.toml` 里标着
  `note = "… verify …"` 的条目（目前是 StepFun 的套餐 URL）取自提供商文档，
  还在等第一次实际调用 —— 错了也只是改一行数据。

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

以上 —— 包括记忆 —— 在 `bench` 里一概**关闭**：公布的分数只衡量 harness 本身，跨任务记忆也会污染 SWE-bench 结果。


## 安全围栏

rockycode 假设你刚克隆下来的仓库可能怀有敌意：

- 项目的 `.mcp.json` **只在你选择信任之后**才启动（`ROCKYCODE_TRUST_PROJECT_MCP=1`），所以克隆来的仓库在启动时保持静止：它的代码在等待，你的 key 留在原处。MCP
  工具描述会做提示词注入扫描。
- `read_file` 拒绝 `.env`、凭据与私钥；工作目录之外的读取需要批准；
  路径 jail 在任何权限模式下都生效。
- 工具输出在进入模型或轨迹日志之前先做密钥脱敏。
- 不受信任的项目配置只能*收紧*工具审批模式。
- 权限模式（`yolo|ask|careful`）与逐命令分类叠加：block 级命令即使在
  yolo 下也会被拒绝；会话级授权仅限单一二进制。
- **修改 rocky 自己的设置，每次都问你** —— 默认模型、提供商地址、模型
  服务器 —— 在任何审批模式下都是如此。`yolo` 把这样的步骤留在 `/parked`
  里等你，其余工作继续。
- **每个子依赖都锁定版本。** 安装包锁定整棵依赖树（默认安装与每个扩展），
  版本就是它自己的测试所用的版本。桌面客户端按精确的 lockfile 安装，
  安装脚本保持关闭。
- **key 存入系统钥匙串**（macOS Keychain、Windows Credential Manager、
  Linux Secret Service，机器上有哪个用哪个）；key 文件里只列出它们的名字。
  0600 权限的 key 文件是另一个选择。
- **决策验证器（Jev / Ollama）**由你主动开启。enforce 只影响本来会询问的调用；shadow 可选择观察 YOLO，只记录判断，执行保持原样。静态 block 仍然优先。
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
`--thinking/--no-thinking`、`--reasoning-effort low|high|max`、
`--max-tokens`、`--context-window`（压缩触发点）、`--max-steps`，以及做
系统提示词 A/B 实验的 `--prompt <文件>`（见 `prompts/README.md`）。

首次运行较慢：HF 数据集下载一次（数百 MB），每个任务还要从 Docker Hub 拉
官方镜像（各约 1 GB，之后永久缓存）。Apple 芯片上请在 Docker Desktop 打开
*"Use Rosetta for x86_64/amd64 emulation"* —— 镜像是 x86 的。

## 架构

- **引擎**（`rockycode/engine/`）—— 与模型、界面都解耦的 Agent 循环。以
  原生工具调用流式访问提供商，执行工具，循环至模型直接给出文字回答为止，对外
  发出带类型的事件流 —— TUI、bench 控制台、JSON-RPC 服务器、轨迹记录器
  都只是订阅者。chat 与 serve 默认不限步数。有限步数的 Engine/bench 运行
  默认 100 次主调用加 10 次收尾调用（`--max-steps` 控制主预算）。中性的
  资源提示为调查和验证留出空间；任务还有剩余时，如实交接。
- **上下文压缩**（`engine/compaction.py`）—— 每次 API 调用前预估下一次
  prompt 大小（最近一次真实的 `prompt_tokens` 加上对新消息的保守估计）。
  到达上下文窗口 50% 时给出一次性提醒；到 90% 时自动压缩：先把旧的工具
  输出桩化（零成本、确定性），需要更多空间时，再用一次 API 调用把更早的历史
  折叠成一份稠密的状态文档，上下文重建为「系统、状态、最近片段」。压缩
  既是事件也是轨迹记录 —— 长任务能扛过窗口，而且改写过程在训练数据里
  始终可见。
- **工具** —— `bash`、`read_file`、`write_file`、`edit_file`、`grep`、
  `glob`，加上 `check_code`（用项目自己的 ruff/pyright，或内置的 pyflakes
  兜底，提供有依据的 lint 与类型反馈）；此外还有 `explore`（只读、引用
  经核验的子调查）、网络工具、记忆工具与 goal 分支审阅工具。chat 与
  bench 共用同一套 schema，只是执行目标不同（本地目录 vs `docker exec`
  进任务容器）。只读工具批次并发执行；任何写操作保持串行。工具输出以
  教会恢复为目标来书写：错误以可读文本返回。
- **Bench runner** —— 每个任务：拉官方镜像 → 起容器 → 智能体在
  `/testbed` 工作 → `git add -A && git diff --cached` 即为预测 → 交给
  `swebench.harness.run_evaluation` 打分。智能体与打分器共用同一镜像。
- **轨迹** —— 每次会话（chat 与 bench）都追加到同一个全局存储
  `~/.rockycode/trajectories/*.jsonl`（`$ROCKYCODE_HOME` 可改位置）：元信息（模型、提示词名称与 sha、
  实例 id）、OpenAI 格式的每条消息、每次调用的用量（含 DeepSeek 缓存
  命中），以及一条结果记录。天生就是 SFT/RL 可用的格式。

### 目录结构

```
rockycode/
├── rockycode/
│   ├── cli/                 # 每个子命令一个模块：chat/exec/config/dream/memory/bench/goal/mcp/serve
│   ├── runtime.py           # 为 TUI 与桌面端组装 Engine + MCP/LSP/记忆/技能
│   ├── headless.py          # `exec`：供其他智能体调用的子代理（JSONL 契约）
│   ├── server.py            # `serve`：面向编辑器客户端的 JSON-RPC 宿主
│   ├── engine/              # 与模型、界面无关的内核，位于最底层（tests/smoke_layering.py）
│   │   ├── loop.py          # 核心循环（事件出、历史入）
│   │   ├── providers.py     # 提供商注册表（DeepSeek、MiniMax、Kimi、GLM …）
│   │   ├── route.py         # 模型的请求发往哪里、用哪个 key —— 只在这里决定一次
│   │   ├── errors.py        # 一次失败的模型请求 = 一句人话 + 下一步
│   │   ├── effort.py        # off/low/high/max 旋钮 → 各提供商档位
│   │   ├── compaction.py    # 上下文压缩（桩化 → 状态摘要）
│   │   ├── tools.py         # 工具 schema + 本地执行 + 路径 jail
│   │   ├── permission.py    # yolo/ask/careful × 风险层级 → 允许/询问/拒绝
│   │   ├── approval.py      # 唯一的审批流水线：策略 → 会话授权 → 循环 tick → 验证器 → 询问宿主
│   │   ├── verifier.py      # 可选的决策模型（Jev / Ollama），只看会询问的那一段
│   │   ├── planmode.py      # 规划模式的只读闸门
│   │   ├── modes.py         # research/learn 模式契约
│   │   ├── mcp.py           # MCP 客户端（stdio 服务 → 额外工具）
│   │   ├── skills.py        # 技能发现 + 渐进披露
│   │   ├── web.py           # web_search / web_research / web_fetch
│   │   ├── container.py     # docker-exec 执行 + 补丁提取
│   │   ├── events.py        # 所有 UI 订阅的事件契约
│   │   └── trajectory.py    # 可直接训练的会话日志
│   ├── goal/                # 目标模式：运行器、会话、分支评审、工作树、预算
│   ├── memory/              # 文件即真相的记忆库 + 语义召回
│   ├── dream/               # 整理、会话评定、弱点挖掘、技能提案
│   ├── routines.py          # 周期性预授权工作（lease 制自动运行）
│   ├── modes/               # research/learn 模式契约（markdown）
│   ├── skills/              # 内置技能（lean-prover、architecture-viz、artifact-design、rocky-setup）
│   ├── i18n/                # 界面文字：英文与中文
│   ├── onboarding.py        # 首次启动与 key 存储（系统钥匙串、0600 key 文件）
│   ├── task_links.py        # 关联任务：会话间的交接与收件箱留言
│   ├── tui/                 # Textual 聊天应用：app.py 是壳层，每个功能一个模块，commands.py 是斜杠命令表
│   ├── runners/             # 裸模型基线 · SWE-bench 智能体 · 共享数据
│   ├── prompts/rocky.py     # 内置系统提示词 + 任务提示词
│   └── score.py             # 封装官方 swebench 评测
├── rockycode-vscode/        # VS Code 扩展（基于 `rockycode serve` 的对话面板）
├── apps/desktop/            # Electron 桌面（源码预览）：基于 rockycode.runtime 的 Python JSON-RPC 宿主
├── prompts/                 # 提示词实验室（A/B 变体）
├── bench/tasks/dev10.json   # 快速迭代子集
└── tests/                   # 离线的冒烟测试（假模型流）
```

## 提示词实验室

系统提示词是可替换的文件。复制 `prompts/rocky-v1.txt`，只改一处，用
`--prompt` 跑 dev10，对比分数、步数、token。名称与哈希处处留痕，每个变体保留自己的预测文件。详见 `prompts/README.md`。

## 开发

冒烟测试套件离线运行、免 key，用脚本化的假模型流端到端驱动引擎：

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
欢迎贡献代码。项目依然处于早期，还有很多粗糙的地方，我们希望先和开发者们多讨论功能和架构设计，再开动。我们有意让它保持小巧：我们预期它是一架改装过的N1星际战机，在某些方面可以推进到极致，这就足够了。

initial commit的贡献来自以下开发者们：  
[@cicialgo](https://github.com/cicialgo)，LLM 算法工程师，负责整体设计和奇怪的实验性功能的添加。  
[@dy2012](https://github.com/dy2012)，LLM 工程与架构师，带来了权限、安全和基于Docker的一系列防护，以及VS Code插件，代码能力增强，以及多个功能的修复  
[@codingmiu](https://github.com/codingmiu)，机器学习研究员，带来了research mode的一系列亮眼设计  
