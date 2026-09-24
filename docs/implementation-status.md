# 岗位数据采集引擎 0.4 实施状态

更新时间：2026-09-18

## 状态口径

- **已完成**：代码、迁移或配置已经实现，并完成与风险相匹配的自动化验证。
- **部分完成**：已有可用实现，但尚未达到该阶段的完整交付门槛。
- **未开始**：可能只有目录、接口或 README 占位，不代表具备可运行能力。

## 已完成能力

### 1. 数据库与基础设施

- 已区分开发和生产数据库拓扑：开发连接既有外部 PostgreSQL；生产设计为控制中心服务器上的独立 PostgreSQL 18.1 容器。
- 生产 PostgreSQL 使用专用 Docker bridge，不发布宿主机 5432，数据卷挂载到 PostgreSQL 18+ 要求的 `/var/lib/postgresql`。
- API 单实例连接池基线为 `pool_size=10`、`max_overflow=0`；API、数据库 Session 和生产数据库统一使用 `Asia/Shanghai`。
- 已生成五个相互独立的初始哈希/加密密钥，保存在被忽略且权限为 `0600` 的本地文件中，不提交、不记录明文。
- 已实现异步 SQLAlchemy Engine、Session Factory、数据库就绪检查和只读准入检查脚本；Alembic 与 API 使用相同的 SSL 和时区参数。
- Alembic `0001` 至 `0009`、SQLAlchemy 2.0 映射、Repository/UoW 和显式提交语义均已实现。
- UoW 在异常或未显式提交的正常退出时回滚；所有 PostgreSQL 集成测试使用外层事务/保存点，测试后 `crawler` 全表保持为空。

### 2. 共享契约与持久化约束

- 共享契约已定义完整 API scope、生产来源注册表、版本化 Agent capabilities、心跳槽位校验、规范化 Job 请求和 `externalRequestId` 规则。
- `0001` 已约束 scope、capabilities、source、Profile key、`request_payload` 和 client 范围内的外部请求 ID。
- `0002` 增加可恢复的 `leased_profile_id`、Profile 外键和活动 Profile 部分唯一索引。
- `0003` 增加 `parsed_record_batches` 幂等回执表，以及按服务端接收顺序分页的记录索引。
- `0004` 增加 Job 一对一回调配置，以 AES-256-GCM ciphertext/nonce 保存 Callback Token，并保存独立 HMAC fingerprint、权威过期时间、凭证版本和加密密钥版本。
- `0005` 删除 Outbox 中重复的 `target_url`，改为引用 Job 回调配置，并以 `(event_id, callback_config_id)` 保证事件投递状态唯一。
- `0006` 增加 Callback Token 轮换幂等历史表，并为 Outbox 增加 pending、credential_blocked、delivered、permanently_failed 状态。
- `0007` 增加可恢复的 Dispatcher 短租约字段和一致性约束，pending 调度索引按状态、到期时间和租约到期时间优化。
- `0008` 增加 Callback 凭证销毁时间和完整状态约束，只允许 ciphertext/nonce 同时存在或在销毁后同时为空。
- `0009` 增加 Outbox 人工重放代次、最近重放时间、永久幂等审计历史和状态分页索引。
- 已在真实 PostgreSQL 18.1 上验证正向迁移、逐版本降级和重新应用；当前 revision 为 `0009`。

### 3. 调用方身份与租户边界

- tenantKey YAML 白名单使用 1 MiB 限制、UTF-8、拒绝重复键的安全解析和严格版本化校验；省略 scopes 时继承 API 全权限集合。
- tenant 配置指纹与顺序无关；tenantKey 以内存哈希和常量时间方式匹配，不提前返回匹配位置。
- API 启动时事务性同步 `api_clients`；被删除或禁用的 tenant 会禁用 client，并在同一事务吊销未过期 Token。
- `POST /v1/auth/token` 只为白名单中已同步且启用的 client 签发 Token，所有凭证拒绝路径统一返回 401。
- 调用方 Token 使用 `cat_` 前缀和 32 字节安全随机值；数据库只保存独立密钥域的 HMAC-SHA256、匹配的 tenantKey 指纹、生命周期和权限快照，明文只返回一次。
- 调用方 Bearer 鉴权会锁定并复核 client/Token，统一拒绝缺失、过期、吊销、错配或 disabled client，并按签发时 scope 快照授权。

### 4. Agent 身份、会话与 Profile

- `provision-agent` CLI 可在无 Agent Session 时预置全局、无 tenant 归属的 Agent，生成唯一 `agentId` 和 32 字节 `deviceSecret`，数据库只保存专用 HMAC。
- `POST /v1/agent/sessions` 只认证已预置 Agent，不提供自注册旁路；未知、disabled 或凭证错误统一返回 401。
- Agent Session Token 使用独立 `AGENT_TOKEN_HASH_KEY`，数据库仅保存 HMAC、签发/过期时间和使用状态。
- `rotate-agent-credential` 和 `disable-agent` 在 Agent 行锁内更新身份并原子吊销未过期 Session。
- Agent Session 鉴权会复核 Session、Agent 状态和请求中的 Agent 身份，防止轮换/禁用并发窗口。
- `POST /v1/agent/heartbeat` 在同一 UoW 更新 Session 最近使用时间、Agent 在线状态、capabilities 和 Browser Profile 元数据。
- Profile 按 `(agent_id, source, profile_key)` upsert；超出离线阈值且未上报的 Profile 标记为 `unavailable`，中心端不存路径、Cookie 或 Profile 内容。

### 5. Job 控制面

- `POST /v1/collection-jobs` 已实现 `collection:write`、`Idempotency-Key`、`202 Accepted`、`Location`、规范化请求快照和按 query 拆分 Task。
- Idempotency-Key 仅保存 `sha256:<hex>`；并发插入使用 `ON CONFLICT DO NOTHING RETURNING` 仲裁，只有获胜事务创建 Task。
- 相同逻辑请求可按 Idempotency-Key 或 `externalRequestId` 重放；相同标识配不同 payload 返回 409。
- `GET /v1/collection-jobs/{jobId}` 已实现 `collection:read`、租户隔离、desired/actual state、固定 Task 状态计数、处理数、失败码汇总和生命周期时间。
- `pause`、`resume`、`cancel` 已实现 `collection:control`、client -> Job -> ordered Tasks 锁顺序、幂等控制和协作式退出。
- queued/paused Task 可立即稳定；活动 Task 进入 `pausing` 或 `cancelling`，保留租约直至 Agent 检查点、上传并安全释放。

### 6. 调度、租约与故障恢复

- `POST /v1/agent/tasks/claim` 只使用已持久化 capabilities 和新鲜 `ready` Profile，不信任 Agent 临时提交的来源列表。
- 调度按 Agent -> Job -> Task 加锁，候选 Job/Task 使用 PostgreSQL `FOR UPDATE SKIP LOCKED`，避免与控制事务形成反向锁顺序。
- 成功领取会原子绑定 Agent/Profile、更新 Job/Task、递增 fencing/attempt、保存 lease HMAC，并追加不可变 attempt。
- `claimRequestId` 同时作为 attempt UUID 和幂等键；活动租约内原样重试返回相同 lease Token 和 Profile，不重复递增计数。
- 活动租约统一校验 Agent Session、Agent/Task/Profile/attempt 绑定、lease HMAC、fencing Token、客户端租约时间和服务端过期状态。
- heartbeat、checkpoint、complete 和 release 已实现续租、单调检查点、控制反馈、终态聚合和 Task/Agent/Profile/attempt 原子清理。
- 过期租约回收按 Agent -> Job -> Task 重新加锁和复核；running Task 重排队，paused/cancelled 意图进入稳定状态，并立即递增 fencing 拒绝迟到写入。
- 当前每次 claim 前回收一个过期候选，正确性已满足；独立周期调度器仍是待实现的运维增强。

### 7. 原始岗位记录与分页

- `payloadHash` 已固定为完整 `ParsedJobFields` 的 SHA-256：camelCase、包含 null、键排序、紧凑 UTF-8 JSON。
- `POST /v1/agent/tasks/{taskId}/records` 复用活动租约鉴权，并校验记录与 Task 的 source、keyword、region、pageLimit 一致。
- PostgreSQL 按 `(collection_task_id, source_url, payload_hash)` 去重；重复观测返回既有 record ID。
- `parsed_record_batches` 保存 batch 请求哈希、有序 `acceptedRecordIds` 和新增数量；原样重试返回首次结果，修改内容或跨 Task 复用 batch ID 返回 409。
- `GET /v1/collection-jobs/{jobId}/records` 已实现 tenant 范围内的 Job 绑定游标，按服务端 `created_at + id` 做 keyset pagination，每页限制 1-200。

### 8. 版本化作业事件

- 事件信封已冻结为 `schemaVersion=1`、eventId、eventType、occurredAt、collectionJobId、externalRequestId、jobVersion 和严格类型 payload。
- 0.4 事件类型固定为 `collection.job.accepted`、`collection.job.desired_state_changed` 和 `collection.job.terminal`；payload 不接受 tenant、client、Agent、Profile、租约或 Token 字段。
- design 文档已提供独立 Outbox 事件目录，逐项说明三类事件的触发次数、payload、回调应用处理方式，以及不会生成 Webhook 的中间状态边界。
- Job 首次创建、控制意图实际变化和首次进入终态时，事件与对应 Job/Task 状态在同一 UoW 事务提交；创建和控制幂等重放不重复生成事件。
- 排队 Job 被立即取消时，同一事务写入 desired-state 与 terminal 事件；两者共享同一 jobVersion，调用方不得依赖同时间 UUIDv4 的业务先后顺序。
- `GET /v1/collection-jobs/{jobId}/events` 已实现 `collection:read`、租户安全 404、Job 绑定游标和 `(occurred_at, id)` keyset pagination，每页限制 1-200。

### 9. Job 回调配置与凭证保护

- 创建 Job 已支持可空 `callback` 对象；Callback URL 支持内网 HTTP 和 HTTPS，必须精确命中当前 tenant 的 `callbackAllowlist`，并拒绝 userinfo、fragment、过期时间越界及 Caller/Callback Token 复用。
- Callback Token 默认有效期为首次成功创建时间加七天；权威过期时间通过 `callbackTokenExpiresAt` 返回，幂等重放不续期。
- `request_payload`、事件、Agent 载荷和 API 响应均不保存或返回 Callback Token；请求校验错误也按敏感字段路径脱敏，回调 URL 和凭证材料只进入专用回调配置表。
- Callback Token 使用 AES-256-GCM、96-bit 随机 nonce 和绑定 schema/config/job/credentialVersion 的 AAD；独立 HMAC-SHA256 fingerprint 只用于幂等比较。
- 已验证相同明文产生不同 nonce/ciphertext、AAD 篡改失败、密钥版本隔离、无明文列、正确解密和事务回滚零残留。
- accepted、desired-state-changed 和 terminal 事件统一通过原子事件入队函数写入；只有配置 Callback 的 Job 才创建 Outbox，首次 `next_attempt_at` 等于事件发生时间。
- Outbox 只保存 `event_id`、`callback_config_id` 和投递状态，不复制 URL、Token、ciphertext 或 fingerprint；幂等重放和未变化的控制操作不会重复入队。
- `PUT /v1/collection-jobs/{jobId}/callback-token` 已实现 `collection:control`、client 范围内 Job 行锁和必需的 `Idempotency-Key`；只能替换 Token 与 expiresAt，不能修改 Callback URL。
- 成功轮换会使用递增后的 credentialVersion 重新执行 AES-256-GCM，原子更新 ciphertext、nonce、fingerprint、权威 expiresAt 和 encryptionKeyVersion，并恢复该 Callback 下未投递的 credential_blocked Outbox。
- 专用轮换历史表只保存 Idempotency-Key SHA-256、规范请求 fingerprint、首次结果版本和权威过期时间；旧轮换键在后续版本产生后仍可确定性重放，不保存明文 Token。
- 终态 Job 的全部 Outbox 均 delivered 后，最后一次结算在 callback 配置行锁内原子清空 ciphertext/nonce 并记录 `credential_destroyed_at`；fingerprint、credentialVersion、expiresAt 和轮换历史保留用于幂等与审计。
- 凭证销毁后禁止新的 Token 轮换；销毁前已经成功的轮换 Idempotency-Key 仍可返回首次结果，不恢复任何凭证材料。

### 10. Webhook Dispatcher

- API 生命周期按配置启动多个 Dispatcher worker；每次使用 `FOR UPDATE SKIP LOCKED` 领取一条到期 pending Outbox，提交短租约后再发起网络请求，不在 Job/事件事务或数据库行锁内等待 HTTP。
- claim 由随机 UUID、领取时间和租约到期时间组成；进程崩溃后可由其他实例在租约到期后接管，旧 claim ID 无法覆盖新持有者的结算结果。
- Dispatcher 仅在内存中短暂解密 Job Callback Token，按冻结事件信封 POST 到 Callback URL，并发送 `Authorization: Bearer` 与 `Idempotency-Key: <eventId>`；不读取回调响应正文。
- 任意 2xx 标记 delivered；401/403 或本地 Token 过期/不可解密标记 credential_blocked；连接错误、超时、408、425、429、5xx 使用有上限的指数退避；其他 4xx 和 3xx 标记 permanently_failed。
- 429/503 支持秒数或 HTTP-date `Retry-After`，同时受配置上限约束；达到最大 HTTP 尝试次数后转为 permanently_failed，并保存非敏感失败分类。
- HTTP 客户端禁止自动跟随 3xx、关闭环境代理读取、设置请求超时，异常只归一化为 timeout 或 connection_error，不记录 URL、Authorization、Callback Token 或响应正文。
- 如果旧 Token 投递返回 401/403 的同时凭证已轮换，结算时比较 credentialVersion；版本变化时保持 pending 并立即使用新凭证重试，避免旧响应重新封锁新凭证。
- delivered 结算串行锁定 callback 配置，并排除当前 Outbox 检查其他未成功投递，避免多 worker 同时完成时遗漏最终凭证销毁；非终态、credential_blocked 或 permanently_failed 均不会提前销毁。
- `/internal/metrics` 已提供独立 Registry 的 Prometheus 指标且不进入 OpenAPI；数据库 Gauge 覆盖 pending、最老年龄、credential_blocked、permanently_failed、retry_exhausted 和活动 claim，进程 Counter/Histogram 覆盖结算结果、耗时、claim 接管、陈旧结算和运行异常。
- Webhook 结算安全审计以单行 JSON 输出固定字段，不记录 Callback URL、Authorization、Token、响应正文或底层异常；Prometheus 标签仅使用固定结果集合。
- 已提供积压、最老 pending、凭证阻塞、永久失败、重试耗尽和频繁 claim 接管的 Prometheus 告警规则样例；指标端点必须由生产网络策略限制为监控网络访问。
- `GET /v1/collection-jobs/{jobId}/webhook-deliveries` 已实现 `collection:read`、租户安全 404、状态绑定 keyset 游标和脱敏运维状态；默认只查询 permanently_failed。
- `POST /v1/collection-jobs/{jobId}/webhook-deliveries/{outboxId}/replay` 已实现 `collection:control`、Idempotency-Key 摘要、Outbox → callback 锁序和保守资格检查；只允许无 claim、凭证仍可用且未过期的 `retry_exhausted_*` 记录。
- 成功受理重放会归零当前轮 attempt_count、递增 replay_count 并恢复完整自动重试预算；永久历史保留此前次数、失败分类和首次结果，同一旧操作键在后续状态变化后仍确定性返回，不重复修改 Outbox。
- Callback 端到端验收已贯通真实 PostgreSQL Outbox、凭证解密、Dispatcher、httpx Transport 和回环 TCP 服务，覆盖 503/Retry-After、断连、读超时、307、401 后轮换恢复和 claim 到期接管。
- 故障测试发现并修复 httpx 默认 INFO 日志输出完整 Callback URL 的旁路；Webhook Transport 现在将 httpx/httpcore 依赖日志固定为 WARNING，应用审计继续只记录稳定标识与失败分类。
- 延迟响应与 claim 接管竞争已验证：新持有者成功结算后，旧请求的迟到结果被 claim ID fencing 拒绝；上游看到相同 eventId 的至少一次请求，数据库只接受一个有效结算。

### 11. Fake Source Adapter 自动化闭环

- 已实现确定性 `FakeSourceAdapter`：固定查询、页码、页内位置和注入时钟产生相同 sourceJobId、URL、字段与 payloadHash；页开始及下一条记录开始前检查控制意图，整页完成后提交累计检查点。
- Fake Adapter 仅在内部测试中模拟 `zhaopin` 协议事实，不新增 `fake` 公共 source，不访问浏览器或网络，也不进入生产数据库约束。
- 真实 PostgreSQL 端到端测试已通过 FastAPI 路由完成 tenantKey 换调用方 Token、Job 提交、Agent Session、心跳/Profile ready、claim、记录上传、批次幂等重放、checkpoint、complete、Job 查询和记录 keyset pagination。
- 测试验证 Agent 响应不含 tenantId、clientId、callback 或调用方 metadata，并比较测试前后全部映射表行数，确认外层事务没有数据残留。
- 该闭环使用 Agent 测试 Actor；生产桌面 Runtime 现在已具备 Transport、Session/claim 循环、SQLite Spool、Supervisor 和 Profile 锁的本地实现，但真实两节点、真实浏览器和断网崩溃验收仍属于阶段 6/8.4，不能由本测试替代。

### 12. Zhaopin Source Adapter

- 已实现共享协议下的 `ZhaopinSourceAdapter`，从旧单体采集器迁移智联搜索 URL、列表/详情选择器、列表字段优先级、招聘人数文本和 sourceJobId 提取，不导入旧 PlatformClient、任务字典、API 仓储或上游业务模型。
- Adapter 使用构造注入的浏览器工厂，默认创建带本机持久化 user-data-dir 的 Chrome/Edge；成功、页面异常和控制退出均进入浏览器关闭路径。Profile 路径只来自执行机配置，不进入 Agent 协议。
- 每页导航前、下一条详情导航前检查控制意图；详情一旦开始读取即完成当前记录并交付，不因中途命令丢弃该记录。整页完成后提交累计 checkpoint；详情加载失败时只保留仍然有效的列表页来源事实，不保存 HTML、截图或错误快照。
- Fake Adapter 与 Zhaopin Adapter 已统一控制粒度；页级 checkpoint 的权威性要求 Agent Runtime 先确认当前页所有记录，Adapter 的 `yield` 本身不等于 API 已接受。
- 岗位 URL 导航前统一为 HTTPS 并限制到 `zhaopin.com` 域，拒绝 userinfo、外部域、fragment 和非标准端口；字段直接映射为 `ParsedJobFields` 并计算规范 payloadHash。
- 默认城市编码只包含旧代码可证实的北京/北京市 `530`；其他地区必须提供经评审的显式映射，不在 Adapter 中猜测。
- 离线 Selenium 兼容 DOM 测试已覆盖完整列表/详情映射、显式空页、详情降级、URL 边界、Profile 校验、控制中止清理、共享协议一致性，以及登录 URL/提示、安全验证信号、无头稳定失败、同一 driver 可见窗口人工继续、超时和等待期间控制中止。
- 8.3 的登录/安全验证检测和人工继续逻辑已完成，但验证依据仍是离线 DOM；尚未声称通过真实智联页面、真实人工登录/验证、Profile 重启复用或双执行机验收，这些属于 8.4。

### 13. Desktop Agent Runtime

- 已实现可启动的 `desktop-collection-agent` 入口：设备凭证换取 Agent Session，后台发送 heartbeat，并在单节点单并发约束下领取任务。
- 已实现 outbound-only `HttpAgentTransport`，覆盖 Session、heartbeat、claim、lease heartbeat、checkpoint、ParsedJobRecord 批量上传、complete 和 release；禁止 HTTP 重定向，网络错误和 408/425/429/5xx 使用有界指数重试，401 交由 Supervisor 刷新 Session 或终止当前租约。
- 已实现 `LocalSpool`：SQLite WAL、`synchronous=FULL`、批次 ID 幂等、有序 pending 重放、上传确认和本地 checkpoint；只保存 ParsedJobRecord，不保存 HTML、Cookie、Profile 内容或任何 Token。
- 已实现 `ProfileLock`：按 source/profileKey 生成稳定本地锁名，使用原子创建、PID/主机标记和死亡进程清理；锁内仅保存本机 WebDriver 进程的 PID/创建时间身份，旧 Agent 确认死亡后才允许精确回收残留树，防止 PID 复用时误杀无关进程。
- 已实现 `BrowserProcessSupervisor`：先执行有界 `driver.quit()`，异常、超时或仍有存活进程时，只终止本任务捕获的 WebDriver 根进程及其子树；进程退出路径返回后才释放 Profile 锁。若无法证明进程树已退出，则保留锁、上报 Profile `unavailable`，并以 `browser_shutdown_failed` 释放控制面任务。
- 已实现 `AgentSupervisor`：单任务浏览器生命周期、Adapter 生产线程、异步上传消费、页级 checkpoint 确认、租约续期、暂停/取消安全点、租约丢失中止和终态资源清理；`SIGINT`/`SIGTERM` 停止新 claim 并协作取消活动任务。
- 已实现托管安装基线：Windows 使用专用交互账号的登录计划任务，Linux 使用 systemd user service + desktop autostart；设备凭证通过系统 keyring/Credential Manager 定位，不进入任务参数、托管配置或标准输出。Windows stop marker 和 Linux SIGTERM 均进入 Supervisor 协作停止，超时后才由托管器兜底终止。
- 本地 Runtime 测试已覆盖 Profile 互斥、spool 幂等、Fake Adapter 完成/暂停/停止、Transport 瞬态失败重试、WebDriver 关闭异常/超时、旧锁残留回收和真实临时子进程终止；真实 API、真实 Chrome/Edge、断网崩溃恢复和两个物理执行机仍必须按 8.4 手册验收。

## 当前验证基线

| 验证项 | 当前结果 |
|---|---|
| Ruff | 通过 |
| 严格 MyPy | 95 个源码文件通过 |
| 本地 Pytest | 293 passed，20 skipped |
| PostgreSQL 集成测试 | 20 passed |
| Alembic 当前 revision | `0009` |
| `crawler` 表数量 | 15 |
| 外键数量 | 19 |
| 关键显式索引 | 9 |
| 集成测试后数据残留 | 所有 `crawler` 表均为 0 行 |

## 开发数据库准入结果

| 检查项 | 结果 |
|---|---|
| 网络可达且认证成功 | 是 |
| 服务端版本 | PostgreSQL 18.1 |
| 应用 Session 有效时区 | Asia/Shanghai |
| 当前开发连接使用 SSL | 是 |
| 服务端最大连接数 | 100 |
| 应用连接池预算 | 10，无 overflow |
| 目标数据库 `crawler` | 已存在 |
| 运行账号可创建数据库 | 否 |
| 运行账号可在 `crawler` 中创建 schema 对象 | 是 |

初次准入检查只执行只读操作。确认目标库中没有用户表、没有 `crawler` schema、
没有 Alembic revision 后，才执行数据库迁移。

## 迁移验证结果

| 检查项 | 结果 |
|---|---|
| 最终 Alembic revision | `0009` |
| `crawler` 表 | 15 张，符合当前预期 |
| 验证后数据行 | 所有表均为 0 行 |
| 外键 | 19 个 |
| 关键显式索引 | 9 个，全部存在 |
| scope/source/payload/capability/callback credential 约束 | 已存在 |
| `api_clients.allowed_scopes` 默认值 | 三项 API 全权限 |
| `api_access_tokens.scopes` 默认值 | 无；签发时必须显式复制 client 权限快照 |
| `collection_agents.capabilities` 默认值 | v1、无来源、一个槽位、headful Chrome |
| 逐版本降级 | `0009 -> 0008` 删除重放历史与 Outbox 重放字段；既有 `0008 -> 0007 -> 0006 -> 0005 -> 0004 -> 0003` 回环仍已验证 |
| 重新应用 | 通过，最终恢复到 `0009` |

降级到 `base` 时保留空的 `public.alembic_version` 表属于 Alembic 正常行为。

## 分阶段交付计划

| 阶段 | 交付范围 | 状态 | 当前结果与剩余门槛 |
|---|---|---|---|
| 0. 数据库环境基线 | 开发准入、生产 PostgreSQL 拓扑、密钥、备份恢复和容量基线 | 部分完成 | 开发 PostgreSQL 18.1 和连接预算已验证，生产拓扑已设计；生产备份恢复、容量、监控和升级演练未完成。 |
| 1. 工程与契约基线 | 包边界、质量门槛、OpenAPI、错误模型、状态机和共享协议 | 部分完成 | 项目结构、Pydantic 契约、Ruff、严格 MyPy 和 Pytest 已启用；CI 与 OpenAPI 兼容性门槛未完成。 |
| 2. Schema 与持久化 | Alembic、ORM、Repository/UoW、锁和迁移验证 | 已完成 | `0001`-`0009`、映射、仓储、回滚纪律和 PostgreSQL roundtrip 已通过。 |
| 3. 两类身份系统 | tenant allowlist、调用方 Token、Agent 预置、Agent Session、轮换和禁用 | 部分完成 | 两套身份域及 tenant/Agent 禁用后的吊销已实现；调用方主动吊销接口和请求限流未完成。 |
| 4. Job 控制面 | 幂等提交、Task 拆分、查询、暂停/恢复/取消和控制事件 | 已完成 | 提交、查询、拆分、租户隔离、协作控制和事务性 collection events 均已通过。 |
| 5. 调度与租约 | capabilities/Profile 匹配、`SKIP LOCKED`、续租、fencing、attempt 和恢复 | 已完成 | claim、重放、续租、检查点、完成/释放和过期回收已通过；独立回收调度器不是当前正确性阻塞项。 |
| 6. 桌面 Agent 通用运行时 | Transport、Session/心跳/claim 循环、SQLite Spool、Supervisor、Profile 锁和进程回收 | 部分完成 | 已实现可启动入口、HTTP Transport（含有界重试）、Session 刷新、heartbeat/claim 循环、SQLite WAL spool、批次重放、PID 复用安全的浏览器进程树回收、Profile 锁、Adapter 安全点控制和两类桌面托管模板；真实 Windows/Linux 安装、断网/崩溃实机恢复和双节点验收仍未完成。 |
| 7. 结果与通知 | 记录写入、分页、事件、Job 回调配置、Outbox 和 Webhook 投递 | 已完成（0.4 范围） | 记录、分页、事件、Callback ciphertext、事务性 Outbox、Token 轮换、Dispatcher、终态凭证销毁、指标、告警、受控重放及真实 HTTP 故障验收已通过；DNS/IP SSRF 防护作为独立延期安全项。 |
| 8. SourceAdapter 与端到端采集 | Fake Adapter、Zhaopin Adapter、登录/验证和双节点验收 | 部分完成 | Fake API/PostgreSQL 闭环、Zhaopin Adapter 代码、离线登录/验证检测与同一窗口等待测试已通过；真实网站人工接管、Profile 重启、线上选择器与双节点验收仍未完成。 |
| 9. 生产就绪 | 镜像、CI/CD、可观测性、备份恢复、压力/安全/故障测试和手册 | 未开始 | 已有 Compose 和 Dockerfile 基线，但尚未执行生产验收门槛。 |

## 任务明细表

以下台账是上述阶段计划的可执行拆分。只有接口或 schema 占位不能标记为已完成。

| ID | 阶段 | 任务与验收结果 | 状态 |
|---|---|---|---|
| 0.1 | 数据库基线 | 验证开发库版本、认证、SSL、时区、schema 权限、连接上限和空库状态 | 已完成 |
| 0.2 | 数据库基线 | 区分开发外部数据库与生产同服务器独立容器拓扑 | 已完成 |
| 0.3 | 数据库基线 | 固定 API 连接池为 10、无 overflow，并统一 `Asia/Shanghai` | 已完成 |
| 0.4 | 数据库基线 | 生成相互独立的 API Token、Agent 设备、Agent Session、Callback Token 加密和 fingerprint 初始密钥 | 已完成 |
| 0.5 | 数据库基线 | 在干净主机验证生产 PostgreSQL 18 镜像、私网、持久卷和一次性 migration 拓扑 | 未开始 |
| 0.6 | 数据库基线 | 演练生产备份恢复、容量阈值、监控、升级和数据卷故障处理 | 未开始 |
| 1.1 | 工程基线 | 在 `refactor/` 初始化 contracts、crawler_api、desktop_agent、tests、docs 和 deploy 边界 | 已完成 |
| 1.2 | 工程基线 | 定义严格 camelCase 传输模型、source/scope 注册表、规范化 payload 和 Agent 边界测试 | 已完成 |
| 1.3 | 工程基线 | 启用 Ruff、严格 MyPy、单元/契约测试和离线 Alembic 编译 | 已完成 |
| 1.4 | 工程基线 | 增加 CI 任务和受版本控制的 OpenAPI/契约兼容性快照 | 未开始 |
| 1.5 | 工程基线 | 冻结公共错误码并验证调用方与 Agent 跨版本兼容性 | 部分完成 |
| 2.1 | 持久化 | 建立身份、Agent、Job、Task、attempt、Profile、record、event 和 Outbox 基线 schema（`0001`） | 已完成 |
| 2.2 | 持久化 | 增加可恢复 Profile 租约绑定和部分唯一约束（`0002`） | 已完成 |
| 2.3 | 持久化 | 增加记录批次回执和服务端顺序游标索引（`0003`） | 已完成 |
| 2.3a | 持久化 | 增加 Job 回调配置表和凭证字段约束（`0004`） | 已完成 |
| 2.3b | 持久化 | Outbox 改为引用 Job 回调配置并删除重复目标 URL（`0005`） | 已完成 |
| 2.3c | 持久化 | 增加 Callback Token 轮换历史和 Outbox 投递状态（`0006`） | 已完成 |
| 2.3d | 持久化 | 增加可恢复 Dispatcher claim、租约一致性约束和 pending 调度索引（`0007`） | 已完成 |
| 2.3e | 持久化 | 增加 Callback ciphertext/nonce 成对销毁状态、销毁时间和迁移回退规则（`0008`） | 已完成 |
| 2.3f | 持久化 | 增加受控重放代次、永久幂等审计历史和状态分页索引（`0009`） | 已完成 |
| 2.4 | 持久化 | 实现异步 ORM、Repository 端口/适配器、显式提交 UoW 和 readiness | 已完成 |
| 2.5 | 持久化 | 验证正向迁移、逐版本降级、重新应用、事务回滚和零残留 | 已完成 |
| 3.1 | 调用方身份 | 加载并严格校验版本化 tenantKey YAML 白名单 | 已完成 |
| 3.2 | 调用方身份 | 同步白名单 client，并在 tenant 禁用/删除时吊销 Token | 已完成 |
| 3.3 | 调用方身份 | 签发仅存 HMAC 的调用方 Token，并显式保存 scope 快照 | 已完成 |
| 3.4 | 调用方身份 | 鉴权 Bearer Token、执行 scope、更新最近使用时间并保持租户隔离 | 已完成 |
| 3.5 | Agent 身份 | 通过受控 CLI 预置无 tenant 归属的 Agent 设备身份 | 已完成 |
| 3.6 | Agent 身份 | 签发并鉴权 Agent Session，不提供 Agent 自注册路径 | 已完成 |
| 3.7 | Agent 身份 | 原子轮换设备凭证、禁用 Agent 并吊销活动 Session | 已完成 |
| 3.8 | 调用方身份 | 实现调用方 Token 主动吊销接口 | 未开始 |
| 3.9 | 身份保护 | 增加认证/请求限流及相应审计和指标 | 未开始 |
| 4.1 | Job 控制 | 幂等创建 Job，并把规范化 queries 拆分为 Tasks | 已完成 |
| 4.2 | Job 控制 | 查询聚合状态、固定 Task 计数并实现租户安全 404 | 已完成 |
| 4.3 | Job 控制 | 实现串行化、幂等的暂停/恢复/取消和协作控制意图 | 已完成 |
| 4.4 | Job 控制 | 在 Job 状态事务内写入 accepted、desired-state-changed 和 terminal 版本化事件 | 已完成 |
| 4.5 | Job 控制 | 幂等创建或控制重放时不重复生成事件 | 已完成 |
| 5.1 | 调度 | 同步经过校验的 Agent capabilities 和 Browser Profile 路由/状态 | 已完成 |
| 5.2 | 调度 | 按 Agent -> Job -> Task 锁顺序和 `SKIP LOCKED` 领取 FIFO 任务 | 已完成 |
| 5.3 | 调度 | 绑定 Agent/Profile、创建不可变 attempt，并签发只存 HMAC 的租约凭证 | 已完成 |
| 5.4 | 租约运行时 | 续租、保存单调检查点并返回当前控制意图 | 已完成 |
| 5.5 | 租约运行时 | 完成或释放任务，并原子清理 Task、Agent、Profile 和 attempt | 已完成 |
| 5.6 | 租约运行时 | 回收过期租约并通过 fencing 拒绝迟到写入 | 已完成 |
| 5.7 | 租约运行时 | 增加独立周期回收器，降低没有 claim 流量时的恢复延迟 | 未开始 |
| 6.1 | 桌面 Agent | 实现控制中心 HTTP Transport、超时、重试、错误映射和秘密脱敏日志 | 部分完成：已实现 outbound HTTP、禁止重定向、超时、状态映射、有界指数重试和内存凭证；结构化 Agent 审计日志及真实网络故障验收待完成 |
| 6.2 | 桌面 Agent | 实现设备认证、Session 刷新、心跳和 claim 循环 | 已完成本地协议闭环：启动换 Session、401 刷新、heartbeat、claim 和单任务并发；真实控制面运行验收待完成 |
| 6.3 | 桌面 Agent | 实现 SQLite spool、有序批次重放、确认和崩溃恢复 | 部分完成：WAL/FULL spool、批次幂等、按任务重放和确认已实现；真实断网/进程崩溃恢复待完成 |
| 6.4 | 桌面 Agent | 实现 Supervisor 和浏览器子进程树生命周期 | 部分完成：Supervisor、协作停止、有界 WebDriver 关闭、精确子进程树回收及交互桌面托管模板已实现；真实 Windows/Linux Chrome/Edge 托管运行验收待完成 |
| 6.5 | 桌面 Agent | 实现本地 Profile 锁并在心跳中对账 Profile 状态 | 部分完成：原子本地锁、PID/创建时间进程身份、旧 Agent 残留回收和 ready/busy/login_required 状态上报已实现；真实 Profile 重启复用和双节点实机验收待完成 |
| 6.6 | 桌面 Agent | 实现安全点暂停/取消、租约丢失中止和本地检查点 | 部分完成：Adapter 安全点、checkpoint 确认、控制意图和租约丢失取消已实现；实机断网和租约接管演练待完成 |
| 6.7 | 桌面 Agent | 实现交互桌面托管安装、系统凭据读取、协作启停、升级和轮换手册 | 部分完成：Windows Interactive 计划任务、Linux systemd user service/autostart、keyring 凭证命令、配置预检、stop marker 和运行手册已实现；真实 Windows/Linux 干净安装和升级回滚验收待完成 |
| 7.1 | 结果 | 校验 ParsedJobRecord 完整性并幂等持久化记录批次 | 已完成 |
| 7.2 | 结果 | 提供稳定、Job 绑定、租户隔离的岗位记录 keyset pagination | 已完成 |
| 7.3 | 事件 | 冻结版本化事件名称、信封和租户安全 payload schema | 已完成 |
| 7.4 | 事件 | 增加事件 Repository，并在 Job 生命周期事务内创建事件 | 已完成 |
| 7.5 | 事件 | 提供 Job 绑定、租户隔离的事件 keyset pagination | 已完成 |
| 7.6 | Webhook | 冻结单 Job Callback Token 创建、默认七天有效期、轮换和投递 HTTP 协议 | 已完成 |
| 7.7 | Webhook | 创建 Job 时接收 callback URL/Token，并禁止 Token 进入请求快照、事件、Agent、响应和日志 | 已完成 |
| 7.8 | Webhook | 新增 Job 回调配置迁移，使用 AES-256-GCM 持久化 Token ciphertext/nonce，并保存本地 HMAC fingerprint、expiresAt 和 credentialVersion | 已完成 |
| 7.9 | Webhook | 用 `callback_config_id` 关联事件级 Outbox，删除重复 `target_url`，并改用 `UNIQUE(event_id, callback_config_id)` | 已完成 |
| 7.10 | Webhook | 实现 Callback Token 轮换接口、credentialVersion 和 credential_blocked Outbox 恢复 | 已完成 |
| 7.11 | Webhook | 按 Bearer、eventId 幂等、HTTP 状态分类和有界退避实现 Dispatcher | 已完成 |
| 7.12 | Webhook | 增加重定向防护、URL allowlist、Token 脱敏/销毁、重试耗尽、指标、受控重放及真实 HTTP 故障测试 | 已完成：503、断连、超时、307、401 轮换恢复、claim 接管、迟到结算 fencing 和依赖日志脱敏均通过 |
| 7.13 | Webhook 安全遗留 | 实现 Callback URL DNS/IP 分类、连接地址固定和 DNS rebinding 防护 | 延期：按既定决策不属于 0.4 验收范围；当前由受控 tenant 完整 URL allowlist、禁止重定向和受信内网边界降低风险，不视为已经解决 |
| 8.1 | Adapter | 实现确定性 FakeSourceAdapter 和 API/Agent 端到端测试 | 已完成：固定输入可重现；真实 HTTP 路由覆盖 Token、Job、Session、心跳、claim、批次重放、checkpoint、complete、聚合与分页，事务回滚零残留 |
| 8.2 | Adapter | 按共享接口实现 ZhaopinSourceAdapter | 已完成：迁移 URL、选择器、字段映射、岗位 ID、持久化浏览器配置、URL 域边界和安全点；15 个离线 DOM 测试通过，线上兼容性留待 8.4 |
| 8.3 | Adapter | 检测登录/安全验证，并允许在同一 Profile 可见窗口中人工继续 | 已完成：离线检测、无头稳定失败、同一 driver/Profile 人工等待、超时、控制中止和状态回调测试通过；真实网站验收留待 8.4 |
| 8.4 | Adapter | 验证 Profile 重启复用、协作控制、租约接管和两个真实桌面 Agent 节点 | 未开始：桌面 Agent Runtime 已具备本地协议实现和 8.4 验收手册 [acceptance-8.4.md](acceptance-8.4.md)，但两台执行机、真实智联页面和实机证据仍未具备 |
| 9.1 | 生产 | 构建并扫描可复现 API 镜像和桌面 Agent 安装包 | 未开始 |
| 9.2 | 生产 | 验证干净生产部署、migration、readiness、回滚和 Secret 注入 | 未开始 |
| 9.3 | 生产 | 增加结构化日志、指标、告警、看板和凭证安全审计 | 部分完成：Webhook 指标、告警规则和结算安全审计已实现；全局日志规范、看板和生产告警接入待完成 |
| 9.4 | 生产 | 执行压力、并发、安全、数据库/API/Agent 重启、断网和故障注入测试 | 未开始 |
| 9.5 | 生产 | 发布部署、Agent 安装、凭证轮换、migration、备份恢复和故障处理手册 | 部分完成：Agent 安装、协作启停、凭证轮换、升级回滚和残留处理手册已提供；API/数据库生产发布、备份恢复及真实执行机演练待完成 |

## 下一实施切片

1. 进入任务 8.4：在两台真实执行机验证智联线上选择器、首次人工登录/安全验证、持久化 Profile 重启复用、协作控制、租约接管和失联恢复；不得依赖控制中心 GUI。
2. 遗留安全项 7.13：在后续安全加固阶段实现 callback URL DNS/IP 分类、连接地址固定和 DNS rebinding 防护；当前以受控 tenant 完整 URL 白名单和禁止重定向降低风险。
