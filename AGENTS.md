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
