# 微信桌面通道结构

本目录把业务编排与微信客户端操作分层，后续替换微信版本或自动化方案时，
应尽量保持上层不变。

## 目录

```text
channel/wechat_desktop/
  config.py                 通道唯一配置源
  models.py                 跨层数据模型
  contracts.py              发送结果、接收回执、公开目标解析契约
  send_control.py           发送调用作用域、取消与提交边界
  triggers.py               扫描和策略层共用的群触发规则
  pipeline/                 通道编排（不碰 UIA 控件）
    channel.py              入口：组合组件、启停和通道兼容接口
    scan.py                 扫描、策略过滤、私聊聚合
    materialize.py          附件物化、引用判断、入队
    reply.py                FIFO 任务准备、分流和终态收尾
    broadcast.py            广播目标展开、任务认领、预写发送
    agent_reply.py          Agent 上下文、回调通知、等待与超时取消
    lifecycle.py            线程安全的耗时诊断记录
    send.py                 最终发送、失败处理、Agent 动作
    prompts.py              提示词、通知文案和纯函数
    fifo_queue.py           全局严格 FIFO
    policy.py               白名单、@、影子模式
    delivery.py             发送门禁、持久化占位与结果归一化
    worker.py               单消费者执行和终态收尾
  uia/                      微信客户端操作
    backend.py              稳定后端接口与工厂
    driver.py               观察结果 → 统一事件；扫描/发送优先级
    client.py               会话身份、基础窗口/控件操作和组件门面
    attachments.py          文件缓存、另存为、附件图片截取
    image_viewer.py         查看器归属验证、安全关闭与主窗口恢复
    reference_resolver.py   定位引用原消息并恢复当前位置
    history_reader.py       独立聊天记录窗口读取
    share_browser.py        分享浏览器窗口、链接与正文读取
    message_sender.py       剪贴板、粘贴提交、分段及发送验证
    controls.py             控件常量与基础转换函数
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

UIA 随机等待统一调用 `_paced_wait(minimum_key, maximum_key)`；节拍默认值只定义在
`config.DEFAULT_CONFIG`，调用点不再附带另一组默认数字。测试通过显式配置覆盖节拍和
业务开关，避免白名单、广播开关等本机配置变化影响回归结果。

`uia/operations.py` 存放可复用的微信动作。目前包含会话选择器解析、文本/图片发送和
统一发送结果。新增微信动作时优先放在这里，通过小而明确的方法暴露给 Driver，
不要把 UIA 定位细节带回 Channel。

## 依赖方向

```text
pipeline/channel.py
   |
   +-> pipeline/{scan, materialize, reply, send, prompts, fifo_queue, policy}
   |                          +-> broadcast / agent_reply
   +-> pipeline/lifecycle（仅诊断）
   |
   +-> config.py / models.py
   |
   +-> uia/backend 接口 <- uia/driver -> uia/operations -> uia/client
   |
   +-> storage/{store, service}
   +-> daily_hot/scheduler -> daily_hot/baidu_hot (~/.cow/.env)
```

## 组件职责与协作边界

`reply.py` 只组织一项 FIFO 任务的准备、分流和收尾。广播、附件引用提示与普通
Agent 回复共用已有队列和发送门禁，不在拆出的组件内创建线程或第二条发送队列。
通道保留显式委托方法，以维持 ChatChannel、调度器和插件使用的入口与签名；
协调器接收通道对象，不导入具体的 `WechatDesktopChannel`，也不通过动态属性转发访问依赖。

| 组件 | 输入与职责 | 状态归属 |
| --- | --- | --- |
| `BroadcastCoordinator` | `enqueue(message, fire_date)` 展开目标并入 FIFO；`send(item)` 认领并发送预写文案 | 广播状态由 `broadcast_jobs` 持久化；实际提交仍走 `DeliveryService` |
| `AgentReplyCoordinator` | `dispatch(event, token)` 构造上下文并交给 Agent；`wait_for_reply(item)` 等待或取消；工具事件负责通知 | 队列令牌属于 FIFO；通知锁和去重集合属于每轮回调闭包 |
| `LifecycleRecorder` | `advance_scan / start / mark / finish` 记录诊断阶段；`snapshot` 返回副本 | 自有锁、时钟、扫描计数及记录表；不修改 `event_runs` 或发送结果 |

Agent 等待超时后先失效令牌，再请求取消；即使取消接口失败，迟到发送仍受令牌门禁约束。
最终持久化终态仍由回复流程结合 `delivery_outcome` 决定，诊断日志不作为重试依据。
生命周期重复标记保留首次时间，结束时原子移除记录，重复结束不重复输出日志。

UIA Client 持有附件读取、图片查看器和引用解析组件，与原有历史读取、分享浏览器、
消息发送组件采用相同的显式委托方式。组件共享注入 Client 的锁、窗口归属校验、
会话身份和基础控件操作，不另建窗口状态或锁。Client 仍保留会话选择、可见消息解析
及基础键鼠操作；附件工作流应写到对应组件，避免继续扩张 Client。

图片查看器只关闭已经验证归属的窗口；引用解析负责定位与恢复，附件读取负责取得
文件或图片。拆分不改变 Driver 的优先级租约、提交前取消检查或不确定发送防重放规则。

## 测试组织

`tests/wechat_desktop/` 按组件组织回归，使用控件替身和临时 SQLite，不操作真实微信。

| 测试模块 | 主要范围 |
| --- | --- |
| `test_broadcast.py` | 广播目标策略、FIFO 入队、预写发送、不确定结果不重放 |
| `test_agent_reply.py` | 上下文筛选、回调通知、超时取消顺序 |
| `test_lifecycle.py` | 首次阶段时间、诊断副本、并发结束、日志幂等 |
| `test_pipeline.py` | 扫描聚合、物化与回复分流的跨组件协作 |
| `test_uia_attachments.py` | 附件缓存、文件与图片读取及相关集成 |
| `test_uia_image_viewer.py` / `test_uia_reference.py` | 查看器安全恢复与引用解析 |
| `test_contracts.py` / `test_correctness.py` | 接收确认、持久化占位、重启防重放与正确性约束 |

热点内容生成及调度器测试仍位于 `tests/test_wechat_desktop_daily_hot.py`。
Windows 回归命令使用项目指定解释器：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe -m pytest tests/wechat_desktop tests/test_wechat_desktop.py tests/test_wechat_desktop_robustness.py tests/test_wechat_desktop_daily_hot.py -q
```

## 迁移约定

- 上层统一使用 `WechatDesktopEvent` 和 `ReplyTargetValidation`，后端不要泄漏控件对象。
- 会话优先使用内部 `conversation_id`；只有在操作边界才解析为标题、runtime ID 和行号。
- 所有发送动作都返回统一的 `accepted_by`、`verification` 和 `observation` 字段。
- UIA 操作必须经过 Driver 的优先级租约，避免后台扫描抢占正在发送的回复。
- 新增解析函数和动作类时应先写不依赖真实微信窗口的单元测试。
- 通道配置只改 `channel/wechat_desktop/config.py` 的 `DEFAULT_CONFIG`；不要在 `pipeline/`、`uia/` 里再维护平行默认配置字典。

## 正确性约束

- 精确会话标识必须在已打开会话的快路径中同样校验；同名会话不通过行号或标题猜测目标。
- 单个会话扫描失败保留其他会话已读取的事件，并通过 `uia_scan_retry_seconds` 主动重试失败会话。
- `triggers.py` 为扫描和策略判断提供同一套群触发规则；只有 `at_only` 可凭缺少 @ 标记提前跳过。
- `send_control.py` 为同步发送链建立独立取消作用域，等待锁、节拍和实际提交前都检查任务状态。
  已有分段提交后中断时返回 `partial` / `uncertain`，不自动重放整条消息。
- 附件原始路径只用于追溯；清理只删除数据库登记、位于通道留存目录且已无事件引用的副本。
  旧数据库里所有权不明的路径不会自动纳入删除范围。

## 状态与接口契约

### 事件接收确认

`observe_events()` 返回的事件在 `acknowledge_events(event_ids)` 前保留在后端内存收件箱。
下一轮首先重交付未确认批次，保持原事件 ID；不继续无限积累新快照。
`event_receipt_capacity` 限制单批接收量，失败及剩余可见消息按 `uia_scan_retry_seconds` 重试。

流水线调用 `store.receive_event(event)` 原子写入 `events` 与 `event_runs`，获得
`EventReceipt(observed_event_id, canonical_event_id, accepted, state)`。
只有 `accepted=True` 才首次路由到 Agent/队列；重复快照返回已存事件的身份。
确认丢失后再次接收不会改写原任务状态，也不会再投递 Agent。存储失败不确认。
ACK 表示已持久化接收或已知重复，不表示已回复成功。事件接收后的路由异常单独记录为 `failed`。

收件箱不是持久化消息队列：进程退出时，尚未落库的快照只能依赖后续 UI 观察重新发现。
UIA 不是微信服务端消息 ID，不能将上述契约理解为跨所有 UI 重建和历史清理的 exactly-once 保证。

### 普通消息的处置规则

正常阶段是 `received -> queued -> running -> 终态`；阶段不能倒退，终态不能被迟到回调覆盖。
`event_runs` 保存阶段、原因与更新时间；`events.processed_at` 保留旧查询的兼容字段。

| 情况 | 持久化终态 | 是否自动重新执行 |
| --- | --- | --- |
| 物化队列或回复 FIFO 满 | `rejected`，原因注明阶段和 `full` | 否；后续重复观察仅确认接收 |
| 等待超过队列时限 | `expired` | 否，不进入 Agent |
| Agent 周期超时，未发现不确定发送 | `timeout` | 否，取消请求并使队列令牌失效 |
| 超时时仍有发送进行中，或已提交但未验证 | `uncertain` / `partial` | 否，抑制额外失败通知与原回复重放 |
| 正常停止时未执行的任务 | `stopped` | 否 |
| 重启发现遗留接收/排队/运行任务 | `interrupted`，原因 `restart_no_replay` | 否 |
| 重启发现未收尾发送记录 | `uncertain` | 否 |
| 明确跳过、缓存或仅观察 | `skipped` / `cached` / `observed` | 否 |

重启收尾仅在旧工作线程全部退出、且新工作线程启动前执行。默认不恢复普通消息队列，
避免重启后集中发送过时回复；需要重新处理时由用户发送一条新消息。
每日热点仍使用独立 `broadcast_jobs`：仅明确没有提交气泡的取消可回到 `pending`，
`partial`、`unverified`、`uncertain` 均不自动重发。
调度器只使用 `enqueue_callback(message, fire_date)`，重试沿用准备日期和缓存文案。
时间解析与配置校验共用 `config.parse_hhmm`。

### 发送结果与防重放

后端和 `DeliveryService` 返回 `SendResult`，保留 Mapping 读取兼容；对外 JSON 使用
`result.to_dict()`。字段包含 `status`、总分段数、已提交/已验证分段数、逐段结果、
`accepted_by`、`verification`、`observation`，以及固定为 `False` 的 `retryable`。

| status | 含义 | 队列终态 |
| --- | --- | --- |
| `sent` | 全部分段已验证 | `completed` |
| `unverified` | 发送动作完成，但未全部验证 | `uncertain` |
| `partial` | 已验证部分分段后中断 | `partial` |
| `uncertain` | 可能已提交，无法判断结果 | `uncertain` |
| `not_sent` | 执行层明确未提交 | `failed` |

门禁失败或提交前取消使用 `DeliveryBlocked` / `SendCancelled`，其语义是尚未提交。
提交过部分气泡后取消必须返回结构化结果，不能抛出上述“未发送”异常。
未知后端异常、无效返回或自相矛盾的结果按 `uncertain` 处理。

生产 Channel 始终向发送服务注入 Store。`deliveries` 在进入后端前先写 `sending`，
完成后写最终结果。同一来源事件集合、目标及输出摘要使用数据库唯一键占位；并发调用、
确认丢失和重启后重复调用均复用已有结果。结果落库失败保留占位，不尝试再发。
父类 `ChatChannel` 的通用发送重试在本通道禁用。没有来源事件 ID 的主动工具请求视为新的
显式请求，但该请求内部同样不执行自动重试。

### 目标解析

后端公开 `resolve_target(conversation) -> TargetResolution`，状态为 `resolved`、
`not_found`、`ambiguous` 或 `stale`；成功时返回 `ConversationTarget` 的不透明 ID 和显示名。
业务层不再读取 `_resolve_selector`、runtime ID 或分析 `uia-session:` 前缀。
失效 ID 不自动退回显示名，同名歧义不猜测，发送前仍由实际 UI 操作验证精确会话。
新后端必须同时实现目标解析与接收确认接口，不能仅实现发送方法。
