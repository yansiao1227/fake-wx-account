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

配置分三层，禁止混放。

### 1. 全局 / Agent 配置（最外层：只用 JSON）

位置：

- 运行时：根目录 `config.json`
- 模板 / 字段样例：根目录 `config-template.json`

放入内容示例：模型与厂商选择（`model`、`bot_type`、`custom_api_base` 等）、
Agent 运行时（`agent`、`agent_workspace`、`agent_max_*`）、全局通道开关
（`channel_type`、`web_console`、`web_host`、`web_port`）、跨通道
`tools` / `skills`、语言与调试等。

说明：

- 最外层用户可见、可改的配置 **以 JSON 为准**；新增或修改全局项时改
  `config.json`，并同步 `config-template.json`。
- 根目录 `config.py` 仅作加载 `config.json`、提供 `conf()` 等运行时基础设施，
  **不是**业务默认配置的存放处，也不是新增全局配置的源；禁止把 wechat_desktop
  等通道配置写进根 `config.py`，也不要把新业务默认值堆在根 `available_setting`
  里当作「配置源」。

### 2. 通道配置（以 wechat_desktop 为例）

位置：

- **唯一配置源**：`channel/wechat_desktop/config.py` 的 `DEFAULT_CONFIG`
  （UIA 节拍、白名单、`shadow_mode`、限流、通知模板、每日热点等**全部**写这里）
- 根目录 `config.json` / `config-template.json`：**默认不要**写 `wechat_desktop` 段
- 若 JSON 中仍出现 `wechat_desktop`，仅作可选覆盖（浅合并到通道默认值之上），
  不应再当主配置清单

规则：

1. 新增或修改通道行为时：只改 `channel/wechat_desktop/config.py`。
2. 禁止把通道配置抄进外层 JSON「图齐全」；外层只保留全局/Agent 与密钥无关项。
3. 禁止在 `wechat_desktop_channel.py`、driver 等业务文件中再维护平行默认配置字典。
4. 禁止把通道细节提升为根 JSON 最外层全局键。
5. 全局/Agent 配置禁止放进 `channel/wechat_desktop/config.py`。
6. 其他通道新增配置时比照本约定，在对应通道目录下维护 `config.py`。

### 3. 密钥与环境变量（`~/.cow/.env`）

**推荐唯一来源**（本工作站）：

```text
C:\Users\26832\.cow\.env
```

跨机统一使用 `~/.cow/.env`（代码中用 `expand_path("~/.cow/.env")`）。

| 项 | 约定 |
| --- | --- |
| 千帆 Key | 环境变量 `QIANFAN_API_KEY`，写在 `~/.cow/.env` |
| 其他厂商 Key | 同样优先 `~/.cow/.env`（如 `OPENAI_API_KEY`） |
| 仓库 `config.json` | 不要把生产密钥当作唯一存储；可留空占位 |
| `config-template.json` | 可保留空字符串字段说明，不填真值 |
| 读取顺序 | 先确保加载 `~/.cow/.env`，再读 `os.environ`；`conf()` 中同名字段仅作可选回退 |

禁止把真实密钥写入文档、日志、提交说明或测试夹具。每日热点、skill、web_search
等凡使用千帆的能力，均按上述顺序解析密钥。
