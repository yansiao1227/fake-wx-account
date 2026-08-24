# 微信桌面通道结构

本目录把业务编排与微信客户端操作分层，后续替换微信版本或自动化方案时，
应尽量保持上层不变。

## 目录

```text
channel/wechat_desktop/
  config.py                 通道唯一配置源
  models.py                 跨层数据模型
  pipeline/                 通道编排（不碰 UIA 控件）
    channel.py              入口：组合 mixin、启停、生命周期
    scan.py                 扫描、策略过滤、私聊聚合
    materialize.py          附件物化、引用判断、入队
    reply.py                FIFO 消费、Agent 投递、每日热点入队
    send.py                 最终发送、失败处理、Agent 动作
    prompts.py              提示词、通知文案和纯函数
    fifo_queue.py           全局严格 FIFO
    policy.py               白名单、@、影子模式
  uia/                      微信客户端操作
    backend.py              稳定后端接口与工厂
    driver.py               观察结果 → 统一事件；扫描/发送优先级
    client.py               窗口、控件、剪贴板、键鼠
    operations.py           可复用动作：选会话、发文本/图
    shell_hook.py           任务栏闪烁唤醒
    group_sender_ocr.py     群聊发送者 OCR
  storage/                  持久化与运行状态
    store.py                会话历史与事件账本
    service.py              给 Web/工具用的状态门面
  daily_hot/                每日热点
    scheduler.py            定时准备并回调入队
    baidu_hot.py            热搜拉取与文案概括
```

## 分层说明

1. `config.py`：通道业务配置的唯一源（含每日热点 `daily_hot_broadcast_*` 与独立
   目标数组 `daily_hot_broadcast_groups`，不复用 `auto_reply_groups`）。根
   `config.json` / `config.py` 只放通用项，不要写 `wechat_desktop` 段；密钥不放这里。
2. `pipeline/`：通道编排层。负责事件聚合、回复队列、Agent 调用、策略检查和
   生命周期记录，不应直接访问 UIA 控件。也挂载每日热点调度器并把预写发送任务
   写入全局回复 FIFO。不维护默认配置字典。
3. `uia/backend.py`：稳定后端接口和创建工厂。新增实现时实现
   `WechatDesktopBackend`，并在 `create_wechat_desktop_backend()` 注册。
4. `uia/driver.py`：UIA 适配层。把微信窗口观察结果转换为统一事件，并协调扫描与
   回复操作的并发优先级。
5. `uia/client.py`：Windows UIA 基础设施层。只处理窗口、控件、剪贴板、键鼠和
   微信 UI 结构，不承担 Agent 或自动回复策略。
6. `daily_hot/`：每日热点定时准备。流程是 `baidu_trending` 取首条 → 百度搜索拉
   详情 → 对话模型做趣味概括/评论 → 文末保留原热搜链接；不直接操作微信窗口，
   只回调 Channel 入队。千帆 Key 从 `~/.cow/.env` 读取；概括优先用全局
   `custom_api_*` 模型。

`uia/operations.py` 存放可复用的微信动作。目前包含会话选择器解析、文本/图片发送和
统一发送结果。新增微信动作时优先放在这里，通过小而明确的方法暴露给 Driver，
不要把 UIA 定位细节带回 Channel。

## 依赖方向

```text
pipeline/channel.py
   |
   +-> pipeline/{scan, materialize, reply, send, prompts, fifo_queue, policy}
   |
   +-> config.py / models.py
   |
   +-> uia/backend 接口 <- uia/driver -> uia/operations -> uia/client
   |
   +-> storage/{store, service}
   +-> daily_hot/scheduler -> daily_hot/baidu_hot (~/.cow/.env)
```

## 迁移约定

- 上层统一使用 `WechatDesktopEvent` 和 `ReplyTargetValidation`，后端不要泄漏控件对象。
- 会话优先使用内部 `conversation_id`；只有在操作边界才解析为标题、runtime ID 和行号。
- 所有发送动作都返回统一的 `accepted_by`、`verification` 和 `observation` 字段。
- UIA 操作必须经过 Driver 的优先级租约，避免后台扫描抢占正在发送的回复。
- 新增解析函数和动作类时应先写不依赖真实微信窗口的单元测试。
- 通道配置只改根目录 `config.py`；不要在 `pipeline/`、`uia/` 里再维护平行默认配置字典。
