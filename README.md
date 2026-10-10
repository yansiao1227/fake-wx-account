# fake-wx-account

基于 [CowAgent](https://github.com/zhayujie/CowAgent) 的 Windows 微信桌面 Agent，支持私聊/群聊自动回复、工具与技能调用、引用资料分析、AI 作图和本地 Web 控制台。

适配微信 **4.1.9.30**；运行环境为 Windows 10/11、PowerShell 5.1+、Conda **`cowagent-wechat`**（本机 Python 3.13.11 x64）。唯一微信后端为 `db_uia`：本机数据库负责接收与查询，UIA 负责发送和必要的附件读取。普通接收不切换窗口、不激活微信、不执行 OCR；发送与附件操作可能激活微信。

## 快速运行

在项目根目录使用 PowerShell，保持微信已登录。所有 Python 命令使用 `cowagent-wechat` 的绝对解释器路径；本机首次运行先验证：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe -c "import sys; print(sys.executable)"
D:\Miniconda\envs\cowagent-wechat\python.exe -m pip install -r .\requirements-windows.txt
if (-not (Test-Path -LiteralPath .\config.json)) {
    Copy-Item .\config-template.json .\config.json
}
```

输出应为 `D:\Miniconda\envs\cowagent-wechat\python.exe`。环境不存在时用 `D:\Miniconda\Scripts\conda.exe env list` 检查；其他机器替换为其 `cowagent-wechat` 解释器路径。

编辑根 `config.json` 配置模型与 Agent，并启用通道：

```json
{
  "channel_type": "web,wechat_desktop"
}
```

按下一节设置微信发送范围后启动：

```powershell
.\cow.ps1 start
```

服务在当前终端前台运行，控制台为 <http://127.0.0.1:9899/chat>；按 `Ctrl+C` 停止。另一个终端可运行 `.\cow.ps1 status`、`.\cow.ps1 stop` 或 `.\cow.ps1 restart`。

`cow.ps1` 需要 PATH 中的 Conda，管理端口固定为 9899；也可直接启动：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe .\app.py
```

## 配置

| 配置层 | 位置 | 内容 |
| --- | --- | --- |
| 全局 / Agent | [config-template.json](config-template.json) → 本机 `config.json` | 模型、Agent、通道启用、Web、通用工具与技能 |
| 微信通道 | [channel/wechat_desktop/config.py](channel/wechat_desktop/config.py) 的 `DEFAULT_CONFIG` | 数据库、私聊/群聊黑名单、群触发、影子模式、节拍、限流与通知策略 |
| 厂商密钥 | `~/.cow/.env` | 环境变量优先，配置字段仅作可选回退；真实密钥不提交 |

根 JSON 不放 `wechat_desktop` 段或微信业务键。修改通道配置后，重启整个 `app.py` 进程。

当前默认 `shadow_mode=False`，私聊和群聊默认准入；`auto_reply_private_blacklist` / `auto_reply_group_blacklist` 均默认为空，分别按会话显示名屏蔽私聊和群聊，被屏蔽的群即使 @ 也不回复。群仍需满足 `group_reply_mode="at_only"` 的 @ 触发，【萌新打怪躺平日记】及其他新群无需添加名单。旧自动回复白名单和全部放行开关已移除。

首次验证可设置 `shadow_mode=True` 禁止发送，正式通道仍可能运行 Agent 或必要附件读取。黑名单在接收路由和所有发送入口检查，屏蔽自动回复、通知及主动发送；只测数据库接收时使用下方专用影子诊断。

`db_data_dir` / `db_account` 为空时自动发现数据目录并绑定唯一账号；账号有歧义时需显式指定。配置详情和故障处理见 [数据库后端说明](docs/wechat-db-backend.md)。

## 使用要点

- **自动回复：** 私聊连续消息先聚合，再进入全局 FIFO；新消息不替换正在执行的任务。微信中可用 `/cancel` 取消、`/steer <指令>` 引导当前 Agent。
- **历史上下文：** 普通回复默认附带最近 3 条同会话微信历史（最多 1500 字符），旧 Agent 对话保留最近 2 轮（最多 1500 字符）。更早或完整内容按需调用 `wechat_history`，默认 20 条、上限 50 条；联系人检索和稳定会话 ID 用法见 [只读工具说明](docs/wechat-db-backend.md#agent-只读工具)。
- **引用资料：** 先在微信引用文字、图片、文件或分享卡片，再输入问题。回复只使用当前问题与直接引用的一层资料；图片/文件需唯一关联后读取，独立图片和分享卡片只观察。网页由 [analyze-url](skills/analyze-url/SKILL.md) 技能读取实际正文，访问受限时说明限制。
- **AI 作图：** 使用 [image-generation](skills/image-generation/SKILL.md) 技能；在 `~/.cow/.env` 配置独立的 `SKILL_IMAGE_GENERATION_*` 凭据，生成图片经现有图片发送权限与目标校验后发送。
- **恢复与边界：** 数据库故障暂停新事件并重试；进程重启不自动重放回复，未确认发送不自动重发。本机数据库仅覆盖已同步的消息，尚未接入朋友圈或媒体数据库解密。

解密缓存与业务账本包含聊天明文，数据库密钥使用 Windows DPAPI 保存。真实附件流程仍需对应微信版本的实机验收；数据位置、读取边界和验证方法见 [数据库后端说明](docs/wechat-db-backend.md)，组件与接口见 [通道架构](channel/wechat_desktop/ARCHITECTURE.md)。

## 诊断与开发

以下命令只验证数据库，影子诊断使用独立账本，不进入 Agent 或发送流程：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\wechat_db_doctor.py --schema
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\wechat_db_doctor.py --shadow-seconds 60
```

账号参数、UIA 诊断与实机验收步骤见 [开发与验收](docs/wechat-db-backend.md#开发与验收)。回归测试使用合成数据库和 UIA 替身：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe -m pytest tests -q
```

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，工作区与中文提交规范见 [AGENTS.md](AGENTS.md)。

## 上游与许可证

通用 Agent 能力来自 [CowAgent](https://github.com/zhayujie/CowAgent)，更多用法见 [CowAgent 文档](https://docs.cowagent.ai/)。项目主体采用 [MIT License](LICENSE)；微信数据库格式参考的来源与范围见 [db/NOTICE](channel/wechat_desktop/db/NOTICE)，对应 [Apache 2.0 许可证](channel/wechat_desktop/db/UPSTREAM_LICENSE.txt)。
