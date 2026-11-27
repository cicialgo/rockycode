# RockyCode 桌面工作台（预览）

[English](README.md)

Electron + React/TypeScript + Python。与终端 UI 共用同一份 Python 核心：选择工作目录、运行任务并实时查看输出、内联审批工具调用、每个任务独立的笔记、HTML 产物预览，以及五模型「田忌赛马」。Agent 循环只在 Python 核心中，桌面是同一个引擎上的一个窗口。

**现状：源码预览。** 从本仓库克隆后直接运行；安装包、签名和自动更新会在之后提供。日常在 macOS 上使用；Windows/Linux 在 CI 中能构建并通过 fixture 检查，实机验收还在等待。

## 从源码运行

需要 Node.js 24（见 `.nvmrc`）和 Python 3.11+。模型的 Key 直接在应用里添加：首次启动的卡片上有一个表单。终端的首次运行（`rockycode`）是另一个入口，两者共用同一份存储。桌面只读取 RockyCode 的用户级配置：项目 `.env` 会被忽略，Key 在界面里始终隐藏。

```sh
git clone https://github.com/cicialgo/rockycode.git && cd rockycode
uv sync                      # Python 核心及依赖，安装到 ./.venv
cd apps/desktop
npm ci --ignore-scripts      # 精确锁文件；安装脚本保持禁用
npm run check:deps           # 来源、完整性、版本钉死、已知恶意版本
npm audit signatures
npm run setup:electron       # 显式下载钉死版本的 Electron 并校验
npm run dev
```

`npm run dev` 以开发布局构建并启动：核心是本仓库根目录，解释器是根目录 `.venv`。其他布局用 `ROCKYCODE_PYTHON` 指定已安装 `rockycode` 的 Python 3.11+ 解释器，`ROCKYCODE_SOURCE` 指定源码根目录：

```sh
ROCKYCODE_PYTHON=/absolute/path/to/python3 npm run dev
npm run dev:fixture          # 离线交互模式：模型与工具都是模拟的
```

`dev:fixture` 在窗口中有明确标识，用模拟事件验证协议与审批流程；真实模型验收要用真实模型。

## 功能

- **目录内的任务。** 任何任务运行前都先通过系统选择器选择工作目录。每个任务有自己的草稿、笔记和模型。停止立即生效；异常退出的任务标记为中断，等你来重新运行。
- **Key。** 首次启动的卡片上有一个表单：选择模型来源，把 Key 粘贴进隐藏输入框。应用用一次免费请求测试它，存入系统钥匙串（有钥匙串时）或 0600 权限的 key 文件，立即可用。之后要添加或更换 Key，运行时对话框里有同一个表单（「添加或更换提供商的 key」）。
- **审批。** 工具调用与终端 UI 使用同一套策略，按任务设置：`ask`（安全读取与目录内写入直接通过，风险工具询问）、`careful`（所有风险/中等调用都询问）、`yolo`（除阻止级命令外全部放行）。在 `yolo` 下，只有你能批准的步骤留在任务里、输入框上方等你，带「现在执行」和「不执行」。「本会话允许」只覆盖那一个 shell 二进制。启动外部 MCP/LSP 进程是单独的同意，会显示服务名、配置来源和启动命令。详见 [docs/TASK_UX.md](docs/TASK_UX.md)。
- **HTML 预览。** HTML 产物在右侧沙箱面板中预览；回复中的本地 HTML 链接可点击；箭头可在默认浏览器中打开。
- **田忌赛马。** 五个已配置模型、同一任务、五个隔离运行，人工评分与可选的匿名模型裁判；导出前完整本地 JSON 预览，文件留在你的机器上。见 [docs/MODEL_RACE.md](docs/MODEL_RACE.md)。
- **更新提醒。** 有新版本时顶部出现横幅，带「打开下载页」和「以后再说」。下载和安装由你决定。启动时检查，每天最多一次；侧栏有关闭开关和「立即检查」，应用菜单有「检查更新…」。
- **界面语言。** 侧栏可选「跟随系统」、English 或中文，立即应用于界面、辅助功能标签与应用菜单，重启后保留；提示词、模型输出、工具数据和你的笔记保持原样。
- **主题。** 侧栏有两个按钮：「跟随系统」随操作系统的浅色或深色设置，「星云」是终端里那套夜空底、紫罗兰色的配色。点一下即切换，重启后保留，并且从第一帧起就是所选主题。

之后提供：定时循环、Dream 与 Goal 界面，以及与终端 UI 的完整功能对齐。已注册的工具仍依赖背后的外部服务，那一部分另行验证。

## 提示词预设

输入框旁的「预设」保存可复用的提示词：从当前草稿保存、新建、改名、改正文或删除。列表对所有任务共用，重启后仍在。使用一条预设会**替换**任务输入框里的文字；输入框里已有不同内容时会先确认。预设只含名称和正文。

文件在桌面用户数据目录的 `prompt-presets.json`（文件 0600，目录 0700），与任务库放在一起，与项目仓库分开，也与存放模型凭证的 `ROCKYCODE_HOME` 分开。

## 隐私

应用自身唯一的网络请求是更新提醒：启动时 GET 一个很小的版本文件 `https://rockycode.ai/desktop/latest.json`，每天最多一次，请求里只有这次获取本身；可在侧栏关闭。模型调用只发往你配置的提供方。统计与崩溃上报在设计上就是缺席的。界面偏好与提醒状态保存在应用的用户数据目录；任务历史保存在该目录下的本地数据库，与项目文件分开。

## 语言文件

词条与终端 UI 共用：`rockycode/i18n/locales/<code>.toml` 的 `[desktop]` 段（应用内去掉 `desktop.` 前缀）加共享的 `[capability_detail]` 段，由仓库根目录的 `python scripts/i18n_desktop.py` 导出为 `src/shared/catalog.ts`（生成文件，已提交；改 TOML 后重新运行）。新增语言 = 新增一个 TOML 文件并重新导出，语言选择器会自动列出。运行时与宿主的消息以词条 key 传输（`error_key`、`detail_key`、JSON-RPC `error.data.key`），按当前语言渲染；未知 key 回退到随行的英文文本。

## 主题

一个主题 = `src/shared/theme.ts` 里的一个名字加一份样式表。星云是 `src/renderer/nebula.css`：开头是一张配色表（取自 `rockycode/palette.py` 和终端的 `rocky` 主题），之后为基础样式表里出现的每一处颜色各写一行。往 `styles.css` 或 `race.css` 加了颜色却没有给星云对应的值时，`tests/nebula.test.ts` 会失败，新组件不会以错误的配色出现。星云是深色主题，选中时主进程会把配色方案固定为深色。

「跟随系统」的深色模式按例外书写：`styles.css` 和 `race.css` 里的深色块只重写在深色页面上不成立的颜色。`tests/dark.test.ts` 会找出没人重写的那些（浅色底、浅色线、深到读不清的文字），新组件不会在深色系统上变成一块白板。

## 验证

```sh
npm run check                                   # 依赖策略、tsc、构建、单元测试
python3 -m unittest discover -s runtime/tests -v  # Python 宿主测试（fixture；无核心环境时核心测试跳过）
npm run test:runtime:shared                     # 强制使用共享核心：Runtime 与完整 Host 集成测试，不调用模型
npm run test:e2e                                # 真实 Electron 窗口 + 离线 fixture
npm run test:e2e:task
npm run test:e2e:race
npm run test:e2e:update                         # 更新提醒，对本地测试源
npm run test:e2e:theme                          # 主题选择；截图写入 artifacts/theme-*.png
```

真实模型验收需要明确传入 `--live`，会产生 API 用量；仅使用合成资料与临时目录，隔离用户 Skills/MCP：

```sh
../../.venv/bin/python3 scripts/test-live-runtime.py --live
npm run test:e2e:live -- --live
```

## 依赖策略

所有安装都禁用 npm 生命周期脚本，`npm ci` 不执行任何代码。版本精确钉死，锁文件是唯一依据；`npm run check:deps` 逐条核对官方源、SHA-512 完整性、钉死版本和已知被投毒的发布。Electron 二进制只由显式的 `npm run setup:electron` 从官方发布源下载并校验。首次启动不会隐式下载任何东西。

## 目录

```
apps/desktop/
├── src/main/        # Electron 主进程：窗口、IPC 校验、Python 宿主生命周期
├── src/preload/     # 渲染进程可调用的命名方法
├── src/renderer/    # React 界面（任务、审批、笔记、HTML 预览、田忌赛马）
├── src/shared/      # 协议类型、i18n 逻辑、生成的词条表
├── runtime/         # 基于 rockycode.runtime 的 Python JSON-RPC 宿主（rockycode_desktop/）
├── scripts/         # 开发/构建/e2e 启动器、依赖检查、Electron 准备
└── docs/            # TASK_UX.md、MODEL_RACE.md
```

渲染进程开启 `contextIsolation` 与 `sandbox`、关闭 `nodeIntegration`，只能使用 preload 暴露的方法；主进程校验每次调用的来源与参数。Python 宿主持有任务、审批、历史与引擎；stdout 只有协议，stderr 只有诊断。
