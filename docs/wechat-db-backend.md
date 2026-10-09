# 微信数据库读取 + UIA 发送后端

`db_uia` 适配 Windows 微信 4.1.9.30 和本机 Conda 环境中的 Python 3.13.11 x64：直接只读解析当前账号本机加密数据库，联系人和历史查询无需切换会话，消息发送继续经过现有 UIA 校验与策略。默认后端仍为 `uia`，不会自动启用数据库读取。

## 职责边界

公共后端接口与工厂位于通道根 `backend.py`，`hybrid.py` 负责组合数据库来源和 UIA 网关，
`binding.py` 负责两种来源之间的账号和会话绑定。`db/source.py` 管理账号生命周期、启动基线、
来源分页与 ACK；`db/reader.py` 负责解密缓存、结构和消息解析。数据库包不依赖 UIA，不通过
窗口或可见正文判断新消息。

`uia/gateway.py` 只提供公开界面操作、目标定位和发送。数据库后端按需创建网关，
不会创建完整的 `WechatUiaDriver` 或其接收扫描状态。默认 `uia` 后端保留原有 UIA 接收，
也复用同一个网关执行发送。标题比较规则由通道根 `conversation.py` 统一维护；旧
`uia/backend.py` 和 `db/backend.py` 只兼容原导入路径。

网关在每个实际 UI 操作段取得优先级租约后重新核验本次发送的账号与目标，保留原有取消、
节拍、分段与发送后气泡确认。生命周期关闭幂等；关闭后的读取或发送不隐式恢复，
通道重新启动时显式 `resume()` 后才恢复使用。
发送入口冻结本次账号生命周期，网关也保存本次操作代次；关闭或重登录后，
旧调用即使遇到通道恢复，也不能继续提交。不可逆发送前同时检查任务取消与网关代次。

## 配置与首次验证

所有微信行为配置只修改 `channel/wechat_desktop/config.py` 的 `DEFAULT_CONFIG`。外层 `config.json` 仅通过 `channel_type` 启用通道；不要增加 `wechat_desktop` 段、数据库路径或通道开关。

先停止现有通道，再在通道配置中设置：

```python
"desktop_backend": "db_uia",
"shadow_mode": True,
"db_data_dir": "",
"db_account": "",
"db_poll_interval_seconds": 1.0,
"db_cache_dir": "",
"db_batch_size": 200,
"db_snapshot_retry_attempts": 3,
"db_key_scan_timeout_seconds": 30.0,
```

空 `db_data_dir` 自动定位本机 `xwechat_files` 数据；空 `db_account` 只在能唯一确定账号时使用。存在多个候选账号且无法确认当前登录账号时会返回错误，应显式指定当前账号及数据目录。`db_data_dir` 可用于指定非默认数据位置，`db_account` 用于约束账号绑定；以状态接口实际报告的账号为准。

保持微信已登录，以 `shadow_mode=True` 做首次验证。先查询联系人与历史，再让现有白名单中的会话收到受控测试消息，检查业务账本中的原生消息身份、消息方向、发送者与重复记录。数据库接收无需切换聊天窗口；发送之前必须确认绑定与读状态。影子模式下不发送真实回复。验证通过后才根据实际需要修改 `shadow_mode`。

## 数据与故障处理

解密缓存默认存放在 Agent 工作区 `wechat_desktop_db`，按账号隔离；`db_cache_dir` 可指定缓存根目录。缓存包含聊天明文，目录权限限制为本机当前用户与系统。每个读取器使用独立 UUID 缓存文件，关闭时删除自己持有的明文副本；DPAPI 加密密钥按账号持久保存，不写入日志、配置或仓库。更换 Windows 用户或机器后需要重新从已登录的微信进程取得密钥。

读取初次全量解密，后续同一 WAL 世代只应用新增有效帧，核验页 HMAC、帧校验和与提交边界。活跃数据库还校验 `-shm` 的两份 WAL 索引头，按其中的已提交帧边界读取，排除回滚后残留的旧尾。刷新快照时持有 Windows SQLite 的共享初始化、WAL 写入与 checkpoint 锁，避免与正在提交或迁移页面的写入交错；全量重建时持锁时间随库规模增加，锁忙时标记陈旧，在下一轮重试。checkpoint、WAL 重置或库替换时重建。失败保留最后验证成功的副本，但标记陈旧、暂停交付新事件并重试；不会自动切回 UIA 接收。联系人与历史查询独立于实时消息游标，不确认事件、不触发自动回复、不写入业务会话历史。

首次启动将已有消息建立为固定历史基线，分页过程中不重设基线；重启补读默认只补账本和历史。只有能够确认启动未读边界时才应用已有启动未读回复策略。进程重启不自动重发此前的回复任务。业务账本中的原生身份包含账号、分片、消息表和主键，连续同文消息及跨分片相同主键仍各自独立。

运行中发现新分片而现有候选密钥无法认证时，按 `db_key_scan_timeout_seconds` 的间隔只读重新扫描；失败期间保留游标。库密钥变化后先重新认证并重建副本，来源代次变化仍会暂停实时交付，避免把替换库中的复用主键当作新消息。

状态接口提供 `db_read_healthy`、`db_read_stale`、`db_read_last_success_at`、`db_read_account_id`、`db_read_backlog`、`db_read_error_code` 与账号绑定信息。出现陈旧、账号歧义或密钥错误时先恢复读取条件，不能据此改用同名联系人发送。

数据库 Reader/Source 的状态只描述读取健康和账号，不能据此断言 UIA 窗口可用。
UIA 网关初始化情况、目标验证方式和跨来源绑定由组合后端报告；数据库健康不表示发送目标
已经通过界面核验。

## Agent 只读工具

```json
{"action": "search_contacts", "query": "联系人或群名称", "limit": 20}
```

通过 `wechat_desktop` 查询联系人，结果返回稳定 `conversation_id`。再调用：

```json
{"conversation_id": "使用查询返回的原始 ID", "limit": 20}
```

这是 `wechat_history` 的参数；指定 `conversation_id` 时完全从数据库查询，不初始化 UIA 网关。
省略 ID 时由网关读取当前已打开会话的标题，唯一映射到数据库会话后查询其历史正文；
无法确定唯一会话时返回错误，不切换窗口或猜测同名目标。默认 20 条，最多 50 条，返回内容、
方向、发送者、原生时间、消息 ID 和来源。原有 `wechat_desktop` 的 `read_history` 动作也接受
可选 `conversation_id`。纯 `uia` 后端不支持按 ID 读取或联系人检索，返回
`capability_unavailable`，不会切换会话或回退查询其他目标。通道暂停时仍遵守现有暂停策略。

数据库会话使用原生 ID，UIA 发送通过实际窗口、RuntimeId、选中状态和标题核验。若界面没有提供原生账号或会话身份，绑定信息明确标记为显示名验证；同名歧义、绑定失效或账号不一致时拒绝发送。

首版支持文本、群发送者、方向、@ 与一层引用。非文本消息保留类型和来源记录；当前无法
可靠关联到 UIA 原生消息身份，附件标记为 `uia_identity_unavailable`，不以正文、显示名或
坐标猜测并下载。此次职责拆分没有增加数据库消息附件获取能力。首版不新增媒体解密、
朋友圈 `sns.db` 分析或朋友圈写入能力。本机数据库只覆盖已经同步到本机的数据。

## 开发与验收

必须使用 `cowagent-wechat` 的绝对解释器；第一次运行 Python 前验证路径：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe -c "import sys; print(sys.executable)"
D:\Miniconda\envs\cowagent-wechat\python.exe -m pip install -r requirements-windows.txt
D:\Miniconda\envs\cowagent-wechat\python.exe -m pytest tests/wechat_desktop -q
```

依赖沿用 `pycryptodome`，增加 `zstandard==0.25.0` 解析压缩正文；不要安装上游完整自动化包。测试使用合成加密库与消息，不提交本机数据库、真实密钥或聊天内容。数据库格式参考 [wechatauto-replica 的固定提交 `fbfb026`](https://github.com/fanyuantaier/wechatauto-replica/tree/fbfb02677f8f5c8d52be166dc2a662a836f1354d)，来源说明见 [NOTICE](../channel/wechat_desktop/db/NOTICE)，上游 Apache 2.0 许可证见 [UPSTREAM_LICENSE.txt](../channel/wechat_desktop/db/UPSTREAM_LICENSE.txt)。

### 只读诊断

在项目根目录运行以下命令可验证账号绑定、数据库解密和结构识别。输出仅包含状态、结构、计数、稳定 ID 与耗时，不输出密钥、联系人名称或聊天内容：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\wechat_db_doctor.py --schema
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\wechat_db_doctor.py --shadow-seconds 60
```

需要指定账号或位置时加 `--data-dir "数据目录" --account "账号目录名"`；`--cache-dir "缓存根目录"` 可指定诊断缓存位置。`--shadow-seconds` 直接观察数据库并落账到账号缓存下独立的 `validation/shadow-ledger.sqlite3`，不运行 Agent、不进入回复流水线、不发送消息。该账本可能含聊天明文，受缓存目录权限保护。诊断默认禁用启动未读回复；正式通道的配置和业务账本不受影响。

影子诊断同时检索联系人并读取一条有消息的会话历史；可用 `--conversation-id "查询返回的稳定 ID"` 指定会话。输出 `cursor_unchanged` 检查历史查询没有推进实时游标。随后统计轮询耗时、CPU 时间、进程文件 I/O 字节数、新增页面解密次数、缓存重建次数与落账消息数。I/O 计数反映进程的文件操作量，不等于物理磁盘实际写入量；新消息延迟按数据库原生时间估算，只有观测到新的实时消息才有数值。

可选的 UIA 对比只读取当前可见会话的 20 条历史，不定位或切换会话，也不发送消息；现有群发送者识别配置启用时可能执行 OCR：

```powershell
D:\Miniconda\envs\cowagent-wechat\python.exe scripts\wechat_db_doctor.py --shadow-seconds 60 --benchmark-uia
```

UIA 读取依赖当前窗口与可见消息状态，单次历史查询耗时不能直接代表持续接收的资源消耗。对比时应记录会话可见范围、OCR 开关与数据库启动成本。

本机验收需要确认选定账号的解密兼容性、影子消息逐条落账、重启恢复与无重复；记录观察延迟、CPU 和磁盘开销，对比 UIA 读取。空闲轮询不应重建缓存，WAL 追加不应全量解密。自动化测试通过不等于本机微信版本和账号验收已经完成。

### 本机只读验证记录

2026-10-08 在微信 4.1.9.30、Python 3.13.11 x64 下，选定账号的联系人、消息和会话库均已解密并通过 SQLite 结构校验。首次解密记录如下，耗时不包含账号定位和初次密钥扫描：

| 数据库 | 文件大小 | 解密页面 | 首次解密耗时 |
| --- | ---: | ---: | ---: |
| `contact.db` | 0.21 MB | 53 | 0.012 秒 |
| `message_0.db` | 5.91 MB | 1514 | 0.119 秒 |
| `session.db` | 0.10 MB | 25 | 0.008 秒 |

加入源库快照锁与 WAL 索引校验后，单库无变化刷新约 2.3–2.5 毫秒。一次 15 秒后台影子观察完成 15 轮，平均每轮 21.8 毫秒、最大 44.8 毫秒，CPU 用时 0.297 秒（约单核 1.98%），进程读取约 1.53 MB，写入 0 字节；没有刷新失败、新增页面解密或缓存重建，没有初始化 UIA 或发送消息。联系人查询返回 50 条（查询上限），选定会话历史返回 20 条、约 36.6 毫秒，实时游标保持不变。初次定位账号与只读密钥扫描约 5.4 秒。

此次观察没有新的入站消息，接收延迟为空；受控新入站、实际 WAL 追加逐条落账和无重复仍需在收到测试消息时验证。可选 UIA 读取在当前窗口状态下返回 `uia_history_unavailable`，没有强制激活窗口，因此未取得可直接比较的 UIA 耗时。WAL 追加、回滚尾复用、批次失败、ACK 丢失和重启补读的正确性由合成库及真实临时 SQLite WAL 测试覆盖，不能替代真实联系人受控消息验收。
