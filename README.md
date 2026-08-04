# fake-wx-account

本项目基于开源项目 [CowAgent](https://github.com/zhayujie/CowAgent) 修改，主要用于在 Windows 上通过 UI Automation（UIA）接入微信 **4.1.9.30** 桌面客户端。

CowAgent 的完整能力、架构、通用部署方式和使用文档请查看：

- 源项目：[zhayujie/CowAgent](https://github.com/zhayujie/CowAgent)
- 官方文档：[docs.cowagent.ai](https://docs.cowagent.ai/)

## 当前版本

当前版本为 **v0.0.1**，已可日常联调与小范围试用。

已具备：私聊/群聊消息读取与自动回复、严格 FIFO 排队、引用附件解析、技能与工具调用、模型 API 失败重试、群聊发送者 OCR、每日热点广播等。稳定性与按用户隔离的长期记忆仍在完善中，**不建议**直接用于要求高可靠性的生产环境。

## 项目特性

### 微信桌面通道

- 仅支持微信 **4.1.9.30** 桌面客户端的私聊和群聊消息读取与自动回复。
- 通过 Windows Shell Hook 唤醒消息观察，并保留定时校准，减少无效轮询和漏消息。
- 联系人/群聊白名单、全部自动回复、黑名单、群聊 `@` 与命令前缀等策略可配。
- 群聊触发须同时满足：会话行出现 `[有人@我]`，且气泡正文含 `@自己的昵称`。
- 群聊发送者优先用 RapidOCR 识别（UIA 仍负责消息身份与正文）；可开关。
- 会话历史持久化，支持去重、保留周期；启动时可处理未读消息（可配）。
- 引用消息最多追溯一层；可见上下文只注入当前消息与被引用原消息；支持文字、图片、文件引用。
- 普通图片与引用图片统一走微信大图查看器提取，失败时才降级截取气泡。
- 单独发送的图片/文件只后台缓存，不隐式挂到后续文字；询问“这是什么图/文件”时需先引用对应附件。
- 发送频率限制、会话冷却、影子模式（只观察不发送）、回复超时与目标复核。
- 每日固定时间向白名单群推送百度热搜首条（子线程准备 → 入全局回复 FIFO）。

### Agent / 工具 / 技能

- 复用 CowAgent 的 Agent、模型、工具、技能、记忆与知识库能力。
- 已接入并启用项目内技能，例如：`baidu-hot-cn`、`amap-lbs-skill`、`docx`、`xlsx`、`pdf-reader`、`image-generation`、`analyze-url`、`ddgs`、`knowledge-wiki`、`skill-creator` 等。
- 内置工具包括文件读写、终端、浏览器、定时任务、记忆检索、联网搜索（`web_search`，可配千帆/DDGS 等）、微信桌面相关工具等。
- 调用 skill/tool 前可发送随机进度提示；图片、Word、PDF、Excel 等可预判附件会在首轮模型请求前提示。
- 进度提示走快速发送通道，不等待普通回复的随机发送间隔，也不推迟最终回复的会话冷却。
- 模型 API 支持有限次指数退避重试；重试耗尽后可向微信发送失败安抚话术；失败请求可落盘便于回放排查。

### 控制台与运维

- 保留本地 Web 控制台，默认地址为 <http://127.0.0.1:9899>。
- Windows 前台管理脚本 `cow.ps1`：`start` / `stop` / `restart` / `status`。

## 配置归属

配置分三层，不要混放：

| 层级 | 位置 | 内容 |
| --- | --- | --- |
| 全局 / Agent | 根目录 `config.json`（模板：`config-template.json`） | 模型与厂商、`channel_type`、Agent 运行时、Web 控制台、跨通道 `tools` / `skills` 等 |
| 微信通道 | **`channel/wechat_desktop/config.py` 的 `DEFAULT_CONFIG`** | UIA 节拍、白名单、`shadow_mode`、限流、进度/失败通知模板、每日热点等 |
| 密钥 | `~/.cow/.env`（本机常见路径：`C:\Users\<用户>\.cow\.env`） | `QIANFAN_API_KEY`、`OPENAI_API_KEY` 等；**不要**把生产密钥提交进仓库 |

说明：

- 根目录 JSON **默认不必**再写 `wechat_desktop` 段；若写了，仅作浅合并覆盖。
- 新增或修改微信通道行为时，只改 `channel/wechat_desktop/config.py`。
- 全局通道启用示例：

```json
{
  "channel_type": "web,wechat_desktop"
}
```

## 运行环境

- Windows 10/11
- 微信 4.1.9.30 桌面客户端（只保留一个已登录主窗口）
- Conda 环境名称：`cowagent-wechat`
- PowerShell 5.1 或更高版本

首次运行可在项目根目录创建环境并安装 Windows 依赖：

```powershell
conda create -n cowagent-wechat python=3.11 -y
conda run -n cowagent-wechat python -m pip install -r .\requirements-windows.txt
Copy-Item .\config-template.json .\config.json
```

随后编辑 `config.json` 配置模型与 API 凭据；编辑 `channel/wechat_desktop/config.py` 配置白名单、影子模式、每日热点等。密钥优先写入 `~/.cow/.env`。`config.json` 含敏感信息时不要提交到版本库。

本机推荐使用固定解释器（与 `AGENTS.md` 一致）：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe -c "import sys; print(sys.executable)"
```

## 启动、暂停与状态管理

在项目根目录执行：

```powershell
# 启动（前台运行，日志直接输出到当前终端）
.\cow.ps1 start

# 查看运行状态
.\cow.ps1 status

# 重启
.\cow.ps1 restart

# 从另一个 PowerShell 窗口停止
.\cow.ps1 stop
```

项目采用前台运行：启动后需保持当前 PowerShell 窗口开启。停止方式：

- 在启动服务的终端按 `Ctrl+C`
- 在另一个窗口执行 `.\cow.ps1 stop`

`cow.ps1` 会检查 `9899` 端口上的进程，只会停止由 `cowagent-wechat` 环境运行的本项目 `app.py`，不会直接结束占用该端口的其他程序。

微信处于风控、白屏或要求重新验证时，请先 `.\cow.ps1 stop`，恢复正常后再启动。

## 消息处理与回复队列

回复调度采用单消费者、严格追加的全局内存 FIFO：

- 私聊消息先按会话进入滑动聚合窗口（默认约 0.5–1.2s，最长等待可配），连续文字与附件合成一批后入队。
- Agent 开始后到达的新消息只排在当前任务之后，不替换、不取消当前任务。
- `/steer <指令>` 与 `/cancel` 绕过聚合与 FIFO，直接引导或取消当前 Agent。
- 扫描阶段只读消息身份与文字；附件在目标确定后按需解析并缓存。发送拥有高于扫描的 UIA 调度优先级。
- 群聊 `@` 规则与私聊批次共用同一严格 FIFO。
- 每条原始消息完成时可输出生命周期汇总（扫描次数、附件解析、Agent/工具/发送耗时等）。

队列只能调度 UIA 观察器已生成的事件。多条消息在同一次扫描前集中到达时，界面可能只暴露最新状态，较早消息仍可能无法分别入队；Shell Hook 未触发时由定时校准兜底。进程停止时，内存中尚未处理的队列不会在下次启动恢复。

## 引用消息与图片处理

收到引用消息时，程序通过微信原生“定位到原文位置”追溯一次原消息，解析完成后使用“回到引用位置”恢复界面。本次注入的可见上下文仅包含当前待回复消息与这一层被引用消息，不会递归解析原消息中的引用。

- **文字引用**：读取原消息完整 UIA 文本，不用可能截断的引用预览当全文。
- **图片引用**：定位原图后与普通图片共用大图查看器流程；不可用时降级截取气泡。
- **文件引用**：定位原文件卡片后，尝试剪贴板路径、下载与微信缓存。
- **失败降级**：原文定位失败时保留引用预览并标记降级，避免把不完整内容当成已成功解析。

相关开关在 `channel/wechat_desktop/config.py`，例如：

```python
"resolve_message_references": True,
"uia_image_viewer_enabled": True,
"uia_file_save_as_enabled": True,
"uia_group_sender_ocr_enabled": True,
```

## 每日热点广播

可在每天固定本地时间，向 `auto_reply_groups` 白名单群推送百度热搜首条。准备在子线程完成，发送任务写入全局回复 FIFO，不打断正在进行的回复。

文案流程：

1. 千帆 `baidu_trending` 取榜单首条（标题、热度、原链接）
2. 千帆百度搜索拉取词条详情
3. 用全局对话模型做事实概括 + 趣味评论（优先 `custom_api_*`，不可用时回退千帆对话，再不行用规则兜底）
4. 消息末尾固定附上原热搜链接

通道配置示例（`channel/wechat_desktop/config.py`）：

```python
"daily_hot_broadcast_enabled": True,
"daily_hot_broadcast_time": "18:00",
"daily_hot_broadcast_tab": "livelihood",  # 与 skill baidu-hot-cn 榜单类型一致
"daily_hot_broadcast_message_prefix": "📰 今日热点",
"auto_reply_groups": ["测试群"],
"shadow_mode": False,
```

说明：

- 千帆密钥写在 `~/.cow/.env` 的 `QIANFAN_API_KEY`。
- `shadow_mode=true`、通道暂停、黑名单或限流时不会实际发送。
- 同一自然日只成功准备并触发一次；拉榜失败会在当日后续 tick 重试。
- 调试时可清除当日状态后重跑：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\clear_daily_hot_state.py --show
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\clear_daily_hot_state.py
```

## 模型失败重试与诊断

全局 `config.json` / `config-template.json` 中可配置：

```json
{
  "model_api_max_retries": 3,
  "model_api_retry_base_seconds": 2.0,
  "model_api_retry_max_seconds": 10.0,
  "model_api_retry_jitter_seconds": 0.5,
  "model_api_failure_messages": [
    "刚才脑内小齿轮打了个滑，我这次没能答上来 😵‍💫 请再戳我一下，我重新来过。"
  ]
}
```

- `model_api_max_retries` 表示首次请求之后的重试次数（默认最多约 4 次总尝试）。
- OpenAI 兼容客户端可将失败请求记录到 `tmp/openai_failed_requests.jsonl`，可用脚本回放：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\replay_openai_failed_request.py
```

微信侧失败安抚话术模板见通道配置中的 `agent_failure_notice_templates`。

## 已完成的阶段目标

### P0：微信消息回复队列（已完成）

严格全局 FIFO、私聊滑动聚合、附件按需物化、`/steer` 与 `/cancel` 旁路、生命周期汇总。详见上文「消息处理与回复队列」。

### P0.5：引用附件与安全发送（已完成）

一层引用解析、大图/文件提取、无引用不隐式挂附件、发送限流与影子模式、进度提示快速通道。

### P1：技能与工具接入（已完成基础能力）

- 项目 `skills/` 下技能可加载并在微信会话中调用。
- 进度提示、预判附件提示、工具事件提示已接通。
- 联网搜索、文档类 skill、百度热搜 skill、高德 LBS 等已可用（依赖对应密钥与环境）。

### P1：模型韧性（已完成）

有限次 API 重试、失败话术、失败请求落盘与回放脚本。

### P1：每日热点广播（已完成）

定时取榜 → 检索详情 → 模型概括评论 → 白名单群 FIFO 发送。

### P1：群聊发送者 OCR（已完成）

RapidOCR 保守匹配气泡上方昵称，作为群聊发送者识别来源。

## 后续计划

### P1：用户画像与个性化输出

- 持久化 Agent 对不同用户的印象和用户画像。
- 按用户生成独立性格、语气与输出风格提示词。
- 用户画像相互隔离，并提供更新、纠正和清除机制。

### P1：按用户隔离的长期记忆

- 为不同用户分别存储和检索长期记忆（避免私聊/群聊/用户间串用）。
- 记忆生命周期管理、清理与隐私控制。
- 在现有 CowAgent 记忆/知识库之上补齐微信侧会话身份映射。

### P2：联系人读取与同步

- 读取并同步微信联系人；稳定映射备注名与昵称。
- 增量更新、变更检测与本地清理。

### P2：好友申请自动审批

- 检测并处理新的好友申请；支持开关、白名单、来源限制与人工确认。
- 审批记录、频率限制与异常自动暂停。

### P2：朋友圈分析与人物画像

- 在授权前提下读取朋友圈，辅助兴趣与表达习惯分析。
- 标注来源、更新时间与置信度；严格隔离各联系人数据。

## 相对源项目的改动

- 主要运行场景收敛到 Windows 微信桌面端，并补充 `uiautomation`、`pywin32`、`rapidocr` 等依赖。
- 增强微信 UIA 控件定位、窗口聚焦、空控件树恢复、会话选择与粘贴发送。
- Shell Hook + 定时校准的消息观察；未读识别、群聊 `@`、发送者与消息方向解析。
- 两级回复队列：全局严格 FIFO + 私聊聚合与附件物化阶段分离。
- 引用消息、大图/文件提取、OCR 群发送者、每日热点调度。
- 通道配置下沉到 `channel/wechat_desktop/config.py`；密钥约定放在 `~/.cow/.env`。
- Windows 前台脚本 `cow.ps1`；UIA 冒烟与会话控件树诊断脚本。
- 移除 CowAgent Electron 桌面客户端、Docker/Linux 部署文件和旧通用启停脚本；本项目统一通过 Windows PowerShell 与 `cow.ps1` 管理。

## 诊断

只读检查微信 UIA 状态：

```powershell
conda run -n cowagent-wechat python .\scripts\test_wechat_uia.py --standard-targets
```

导出会话列表控件树（含原始会话名称与消息预览）：

```powershell
conda run -n cowagent-wechat python .\scripts\dump_wechat_conversation_tree.py --output .\tmp\wechat-conversations.json
```

查看或清除每日热点状态：

```powershell
conda run -n cowagent-wechat python .\scripts\clear_daily_hot_state.py --show
```

除非已确认测试对象和发送内容，否则不要使用会实际发送微信消息的测试参数。诊断报告可能含真实聊天内容，对外转发前请脱敏。

## 许可证

本项目沿用 CowAgent 的 [MIT License](./LICENSE)。使用本项目时请同时遵守微信客户端及相关服务的使用条款，并自行评估桌面自动化带来的账号风险。
