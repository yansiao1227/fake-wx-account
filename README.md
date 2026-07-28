# fake-wx-account

本项目基于开源项目 [CowAgent](https://github.com/zhayujie/CowAgent) 修改，主要用于在 Windows 上通过 UI Automation（UIA）接入微信 4.x 桌面客户端。

CowAgent 的完整能力、架构、通用部署方式和使用文档请查看：

- 源项目：[zhayujie/CowAgent](https://github.com/zhayujie/CowAgent)
- 官方文档：[docs.cowagent.ai](https://docs.cowagent.ai/)

## 当前版本

当前版本为 **v0.0.1**，处于早期可用阶段。目前已具备基本的微信消息读取与自动回复能力，但稳定性、并发消息处理、用户记忆和技能支持仍需继续完善，不建议直接用于要求高可靠性的生产环境。

## 项目特性

- 复用 CowAgent 的 Agent、模型、工具、技能、记忆与知识库能力。
- 仅支持微信 4.1.9.30 桌面客户端的私聊和群聊消息读取与自动回复。
- 支持联系人/群聊白名单、全部自动回复、黑名单、群聊 `@` 与命令前缀等策略。
- 保存会话历史，为回复提供上下文，并支持历史数据清理与去重。
- 提供发送频率限制、会话冷却、影子模式和紧急停止热键等安全措施。
- 保留本地 Web 控制台，默认地址为 <http://127.0.0.1:9899>。

## 运行环境

- Windows
- 微信 4.1.9.30 桌面客户端
- Conda，且环境名称必须为 `cowagent-wechat`
- PowerShell 5.1 或更高版本

首次运行可在项目根目录创建环境并安装 Windows 依赖：

```powershell
conda create -n cowagent-wechat python=3.11 -y
conda run -n cowagent-wechat python -m pip install -r .\requirements-windows.txt
Copy-Item .\config-template.json .\config.json
```

随后按需编辑 `config.json`，配置模型、API 凭据和 `wechat_desktop` 选项。`config.json` 包含敏感信息，不应提交到版本库。

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

项目采用前台运行方式，启动后需要保持当前 PowerShell 窗口开启。暂停/停止服务还可以使用以下方式：

- 在启动服务的终端按 `Ctrl+C`。
- 按紧急停止热键 `Ctrl+Alt+Shift+Q`（可在 `config.json` 中修改或关闭）。
- 在另一个 PowerShell 窗口执行 `.\cow.ps1 stop`。

`cow.ps1` 会检查 `9899` 端口上的进程，只会停止由 `cowagent-wechat` 环境运行的本项目 `app.py`，不会直接结束占用该端口的其他程序。

## 相对源项目的改动

- 将主要运行场景收敛到 Windows 微信桌面端，并补充 `uiautomation`、`pywin32` 依赖。
- 增强微信 UIA 控件定位、窗口聚焦、空控件树恢复、会话选择以及粘贴发送流程。
- 使用 Windows Shell Hook 唤醒消息观察，同时保留定时校准，减少无效轮询和消息遗漏。
- 改进未读会话识别、群聊 `@` 判断、发送者与消息方向解析，并通过微信聊天记录辅助识别本人消息。
- 增加随机化操作等待、发送间隔、单会话冷却、回复超时、频率限制和回复目标复核。
- 增加会话历史持久化、去重、保留周期和公众号内容学习相关配置。
- 提供 Windows 前台管理脚本 `cow.ps1`，支持 `start`、`stop`、`restart`、`status` 和紧急停止热键。
- 增加微信 UIA 冒烟测试及会话控件树诊断脚本。

## 后续计划

### P0：群聊消息队列重构的思考

- 问题：“跨会话 FIFO + 同会话 latest-wins 合并 + 正在处理时抢占”。

| 场景                           | 当前行为                                                   |
| ------------------------------ | ---------------------------------------------------------- |
| 其他会话的新消息               | 通常会进入 FIFO 队列                                       |
| 同一会话已有待处理消息         | 新消息会替换旧的待处理消息                                 |
| 同一会话正在回复               | 当前回复会被标记为 `superseded` 并尝试取消，新消息重新入队 |
| 多条消息在下一次扫描前集中到达 | 扫描只选择最新一条符合条件的消息，较早消息不会分别生成事件 |
| UIA 正在扫描或发送             | 监听暂时等待共享锁，完成后再扫描                           |
| Shell Hook 未触发              | 最迟等待定时校准，但仍可能只能看到最新消息                 |

### P1：技能支持

- 接入并完善 CowAgent 的技能系统。
- 支持按需启用技能，并在微信会话中安全调用。

### P1：用户画像与个性化输出

- 持久化 Agent 对不同用户的印象和用户画像。
- 根据不同用户生成独立的性格、语气和输出风格提示词。
- 确保用户画像相互隔离，并提供更新、纠正和清除机制。

### P1：按用户隔离的长期记忆

- 为不同用户分别存储和检索长期记忆。
- 防止私聊、群聊及不同用户之间出现记忆串用。
- 支持记忆的生命周期管理、清理和必要的隐私控制。

### P2：联系人读取与同步

- 读取并同步微信中的全部联系人。
- 建立稳定的联系人标识、备注名和昵称映射，为用户画像与独立记忆提供关联依据。
- 支持增量更新、联系人变更检测和本地数据清理。

### P2：好友申请自动审批

- 检测并处理新的好友申请。
- 支持自动审批开关、审批白名单、来源限制和人工确认模式。
- 保存审批记录，并提供频率限制和异常情况下的自动暂停机制。

### P2：朋友圈分析与人物画像

- 在获得授权的前提下读取联系人的朋友圈内容。
- 根据公开内容分析用户的兴趣、表达习惯和性格倾向，作为个性化回复的辅助信息。
- 标注画像的来源、更新时间和置信度，允许人工纠正或删除。
- 对朋友圈内容、分析结果和不同联系人的画像进行严格隔离，避免隐私数据泄露或跨用户误用。

## 诊断

只读检查微信 UIA 状态：

```powershell
conda run -n cowagent-wechat python .\scripts\test_wechat_uia.py --standard-targets
```

导出包含原始会话名称和消息预览的会话列表控件树：

```powershell
conda run -n cowagent-wechat python .\scripts\dump_wechat_conversation_tree.py --output .\tmp\wechat-conversations.json
```

除非已确认测试对象和发送内容，否则不要使用会实际发送微信消息的测试参数。

## 许可证

本项目沿用 CowAgent 的 [MIT License](./LICENSE)。使用本项目时请同时遵守微信客户端及相关服务的使用条款，并自行评估桌面自动化带来的账号风险。
