# Agent 工作区规则

本文件中的规则适用于整个仓库。

## 必须使用的 Python 环境

- 本项目统一使用 Conda 环境 `cowagent-wechat`。
- 当前工作站对应的 Python 解释器为：
  `D:\Miniconda\envs\cowagent-wechat\python.exe`。
- 执行任何 Python 相关命令时，都必须显式使用上述解释器，包括运行脚本、模块、
  pip、pytest、compileall 和内联 Python。
- 禁止直接使用裸命令 `python`、`pip` 或 `pytest`，因为它们可能指向 Miniconda
  的 `base` 环境。
- 禁止在本项目中使用 `C:\python\python.exe`。

每个需要使用 Python 的任务，在第一次执行 Python 命令前，必须运行：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe -c "import sys; print(sys.executable)"
```

输出路径必须是：

```text
D:\Miniconda\envs\cowagent-wechat\python.exe
```

如果环境不存在或输出路径不一致，使用
`D:\Miniconda\Scripts\conda.exe env list` 查找环境。不得为了临时绕过问题而把
依赖安装到 `base` 环境。

## Python 命令写法

运行脚本：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\example.py
```

运行测试：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe -m pytest tests -q
```

安装或检查依赖：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe -m pip install -r requirements-windows.txt
D:\Miniconda\envs\cowagent-wechat\python.exe -m pip show rapidocr
```

新增 Python 依赖时，必须同步更新仓库中对应的 requirements 文件，并通过上述
解释器执行 `python -m pip` 安装。选择依赖版本、wheel 或环境标记时，必须考虑
`cowagent-wechat` 当前使用的 Python 版本。

## 配置归属（必须遵守）

配置分三层，禁止混放。判断标准：**外层只放跨通道通用项；某个通道自己的行为、策略、
节拍、白名单、文案模板，一律放该通道内部的 `config.py`。**

### 1. 全局 / Agent 配置（外层：JSON 为源，根 `config.py` 只负责加载）

位置：

- 运行时：根目录 `config.json`
- 模板 / 字段样例：根目录 `config-template.json`
- 根目录 `config.py`：只加载 JSON、提供 `conf()`，**不是**业务默认值仓库

外层 **只允许** 通用项，例如：

- 模型与厂商：`model`、`bot_type`、`custom_api_base`、各厂商 `*_api_key` / `*_api_base`
  （本 fork 相对上游 CowAgent 新增的 `model_api_max_retries` 等重试项写在
  `channel/wechat_desktop/config.py`，不要加回根 `available_setting`）
- Agent 运行时：`agent`、`agent_workspace`、`agent_max_*`、`enable_thinking`
- 进程与控制台：`channel_type`、`web_console`、`web_host`、`web_port`、`cow_lang`、`debug`
- 跨通道能力：`tools`、`skills`、`mcp_servers`、语音引擎名等

`channel_type` 只表示「启用哪些通道」，不是微信业务配置。启用桌面微信时外层写：

```json
{
  "channel_type": "web,wechat_desktop"
}
```

说明：

- 新增或修改通用项时改 `config.json`，并同步 `config-template.json`。
- 禁止把 wechat_desktop 等通道业务写进根 `config.py` 的 `available_setting`。
- 禁止在根 `config.json` / `config-template.json` 写 `wechat_desktop` 段，也禁止把
  白名单、`shadow_mode`、UIA 节拍、每日热点、通知模板等提升为最外层全局键。
- 根 `config.json` 里若仍残留 `wechat_desktop` 段，或把通道 `DEFAULT_CONFIG` 里的键
  （如 `shadow_mode`、`auto_reply_groups`）写到最外层，加载时删除并打警告，不得再当覆盖源。

### 2. 通道业务配置（以 wechat_desktop 为例）

位置：

- **唯一配置源**：`channel/wechat_desktop/config.py` 的 `DEFAULT_CONFIG`
- 白名单、`shadow_mode`、UIA 节拍、限流、进度/失败通知模板、每日热点、引用/附件策略等
  **全部**写这里

规则：

1. 新增或修改微信桌面行为时：只改 `channel/wechat_desktop/config.py`。
2. 禁止把通道业务配置抄进外层 JSON「图齐全」。
3. 禁止在 `pipeline/`、`uia/`、`storage/`、`daily_hot/` 等业务文件中再维护平行默认配置字典。
4. 全局/Agent 通用项禁止放进 `channel/wechat_desktop/config.py`。
5. 其他通道比照本约定，在对应通道目录下维护自己的 `config.py`。

### 3. 密钥与环境变量（`~/.cow/.env`）

**推荐唯一来源**（本工作站）：

```text
C:\Users\26832\.cow\.env
```

跨机统一使用 `~/.cow/.env`（代码中用 `expand_path("~/.cow/.env")`）。

| 项 | 约定 |
| --- | --- |
| 千帆 Key | 环境变量 `QIANFAN_API_KEY`，写在 `~/.cow/.env` |
| 豆包搜索 Key | 环境变量 `WEB_SEARCH_API_KEY`（或 `tools.doubao_search.api_key`），写在 `~/.cow/.env` |
| 专业数据集 Agent Plan | `AGENT_PLAN_API_KEY`（可与 `WEB_SEARCH_API_KEY` 同为订阅 Ark Key），写在 `~/.cow/.env`；MCP 配置在 `~/cow/mcp.json` 的 `datapro`（工具 `dataPro_search`） |
| 图片生成 Seedream / 方舟 | 环境变量 `ARK_API_KEY` + `ARK_API_BASE`（Agent Plan 用 `https://ark.cn-beijing.volces.com/api/plan/v3`）；无 `ARK_API_KEY` 时回退 `AGENT_PLAN_API_KEY` |
| 其他厂商 Key | 同样优先 `~/.cow/.env`（如 `OPENAI_API_KEY`、`ARK_API_KEY`） |
| 仓库 `config.json` | 不要把生产密钥当作唯一存储；可留空占位 |
| `config-template.json` | 可保留空字符串字段说明，不填真值 |
| 读取顺序 | 先确保加载 `~/.cow/.env`，再读 `os.environ`；`conf()` 中同名字段仅作可选回退 |

禁止把真实密钥写入文档、日志、提交说明或测试夹具。每日热点、skill、web_search、
baidu_ai_search（智能搜索）、豆包搜索等凡使用云厂商密钥的能力，均按上述顺序解析密钥。
