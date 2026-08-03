# 微信桌面通道结构

本目录把业务编排与微信客户端操作分成四层，后续替换微信版本或自动化方案时，
应尽量保持上层不变。

1. `wechat_desktop_channel.py`：通道编排层。负责事件聚合、回复队列、Agent 调用、
   策略检查和生命周期记录，不应直接访问 UIA 控件。
2. `backend.py`：稳定后端接口和创建工厂。新增实现时实现
   `WechatDesktopBackend`，并在 `create_wechat_desktop_backend()` 注册。
3. `uia_driver.py`：UIA 适配层。把微信窗口观察结果转换为统一事件，并协调扫描与
   回复操作的并发优先级。
4. `uia_client.py`：Windows UIA 基础设施层。只处理窗口、控件、剪贴板、键鼠和
   微信 UI 结构，不承担 Agent 或自动回复策略。

`operations.py` 存放可复用的微信动作。目前包含会话选择器解析、文本/图片发送和
统一发送结果。新增微信动作时优先放在这里，通过小而明确的方法暴露给 Driver，
不要把 UIA 定位细节带回 Channel。

## 依赖方向

```text
Channel -> Backend 接口 <- UIA Driver -> Operations -> UIA Client
   |              |
   +-> Policy     +-> Models
   +-> FIFO Queue
   +-> Store
```

## 迁移约定

- 上层统一使用 `WechatDesktopEvent` 和 `ReplyTargetValidation`，后端不要泄漏控件对象。
- 会话优先使用内部 `conversation_id`；只有在操作边界才解析为标题、runtime ID 和行号。
- 所有发送动作都返回统一的 `accepted_by`、`verification` 和 `observation` 字段。
- UIA 操作必须经过 Driver 的优先级租约，避免后台扫描抢占正在发送的回复。
- 新增解析函数和动作类时应先写不依赖真实微信窗口的单元测试。
