# 岗位数据采集引擎 0.4 实施计划

更新日期：2026-09-17

## 1. 实施目标

交付一个不依赖任何具体上层业务系统的岗位数据采集引擎，包括：

- 容器化、无管理 GUI 的 `crawler-api` 控制中心。
- 开发环境使用既有 PostgreSQL；生产环境在控制中心服务器部署独立 PostgreSQL 容器。
- tenantKey 白名单、调用方短期 Token 和数据边界。
- 控制中心侧 Agent 身份预置、Agent 短期 Session 和凭证轮换。
- 至少两个无 tenant 绑定的桌面 Agent 节点。
- 异步作业、暂停、恢复、取消、租约、fencing token 和故障恢复。
- 解析后原始岗位记录、事件和 Webhook Outbox。
- 一个通过真实执行机验证的招聘来源 Adapter。

本项目不实现当前平台适配、上层业务回调处理、管理 GUI、跨来源岗位主数据、
业务分析，以及中心端 HTML、截图、Cookie 或 Browser Profile 存储。

## 2. 已确定的部署基线

| 项目 | 0.4 基线 |
|---|---|
| API | 控制中心服务器上的单个 `crawler-api` 容器 |
| 开发数据库 | 复用既有 PostgreSQL，目标为 `10.100.11.182:5432/crawler` |
| 生产数据库 | PostgreSQL 18.1 容器，位于控制中心服务器；卷挂载到 18+ 要求的 `/var/lib/postgresql` |
| 生产网络 | 与 API 加入专用 bridge，不发布宿主机 5432，内部链路不启用 TLS |
| API 数据库连接池 | 每个 API 实例基准容量 `pool_size=10`、`max_overflow=0`；连接按需建立 |
| 时区 | API、数据库 Session 和生产 PostgreSQL 统一为 `Asia/Shanghai` |
| 数据库凭证 | 由部署 Secret 注入；真实密码不进入仓库、镜像、日志和文档 |
| Migration | 开发和生产均使用相同 API 镜像运行一次性 `alembic upgrade head` 容器 |
| Agent | 至少两个独立桌面节点，分别持有唯一设备凭证和本地 Profile |
| Agent 归属 | 全局执行资源，不绑定 tenant，不接收调用方上下文 |

开发数据库连接信息中尚未给出可验证的 SSL 结论，实施阶段应探测服务端能力并
确定开发环境 `POSTGRES_SSLMODE`。生产使用同机专用 Docker bridge，按 0.4 方案
配置 `POSTGRES_SSLMODE=disable`；该结论不反向适用于开发外部连接。

## 3. 开工前冻结项

以下决策必须在生产 migration 前完成评审：

1. 开发数据库版本、SSL 模式、账号权限、隔离 schema 和使用约束。
2. PostgreSQL 18 的具体镜像补丁版本、全局连接数预算和 statement timeout；应用单实例连接池已固定为 10。
3. 生产数据卷、备份、恢复、容量、监控、升级和故障通知责任人。
4. Agent MVP 单机并发数。0.4 建议固定为 1，避免与单值 `current_task_id` 冲突。
5. 任务租约、续租周期、离线阈值、最大尝试次数和人工验证超时。
6. 岗位记录保留期、单作业 query 上限、页数上限和批次大小。
7. Webhook callback URL allowlist、单 Job 短期 Token、七天有效期、控制面 ciphertext、Token 轮换、重试窗口和 SSRF 防护规则。
8. tenantKey、deviceSecret、访问 Token 和 Agent Token 的轮换/吊销流程。

## 4. Agent 信任引导实施顺序

Agent 身份预置与运行时会话是两个不同阶段，不能合并为自注册流程。

### 4.1 安装前预置

1. 授权安装流水线在控制中心侧执行非 HTTP `provision-agent` CLI。
2. CLI 使用密码学安全随机源生成 `agentId` 和至少 256 bit 的 `deviceSecret`。
3. CLI 使用独立的 `AGENT_DEVICE_CREDENTIAL_HASH_KEY` 对 deviceSecret 计算 keyed hash；不得复用 Agent Session Token hash key。
4. CLI 在事务中写入 `collection_agents(id, display_name, device_credential_hash, status=offline)`；`capabilities` 使用版本化数据库默认值：`schemaVersion=1`、空 `supportedSources`、`maxConcurrentTasks=1`、headful Chrome。Agent 认证后的心跳再以实测能力覆盖该值。
5. CLI 将明文凭证通过受控安装通道一次性交付给指定执行机。
6. 执行机把凭证写入操作系统凭据存储，安装日志不输出秘密。
7. 运维系统记录操作者、机器标识、agentId、时间和结果，但不记录明文秘密。

当前托管实现使用 `desktop-collection-agent-credential` 通过系统 keyring 写入凭证：
Windows 使用当前专用账号的 Credential Manager，Linux 使用当前图形会话中已解锁的
Secret Service。托管配置仅保存 keyring service/username，不保存 deviceSecret；
`desktop-collection-agent-check-config` 在启动前验证配置和凭证可读性且不输出秘密。

由于 Agent 必须显示浏览器并允许人工操作，Windows 使用当前专用账号的 Interactive
登录计划任务，不能安装为 Session 0 Windows Service；Linux 使用 systemd user service
并由桌面 autostart 注入 DISPLAY/Wayland/DBus 环境，不能安装为无桌面的系统级 daemon，
也不能启用 user lingering。完整启停、升级、回滚和凭证轮换见
`deploy/agent/RUNBOOK.md`。

该阶段使用部署/运维权限和数据库连接，不使用 Agent Session，不调用公共 Agent
注册 API，也不创建 `api_clients`。

### 4.2 运行时建会话

1. Agent 首次启动后调用 `POST /v1/agent/sessions`。
2. API 按 agentId 查询已经存在的 `collection_agents`。
3. 未找到、disabled 或凭证不匹配时统一拒绝，不自动创建 Agent。
4. 验证成功后写入 `agent_sessions`，数据库只保存 Session Token hash。
5. 明文 Agent Token 仅在响应中返回一次。
6. 心跳、领取、续租、上传和完成接口均要求有效 Agent Token。

必须同时提供 `rotate-agent-credential` 和 `disable-agent` 运维命令。轮换时先吊销该
Agent 的现有 Session，再一次性交付新 deviceSecret。

## 5. 分阶段交付计划

### 5.1 已冻结的协议初值

- API 全权限集合固定为 `collection:write`、`collection:read`、`collection:control`。tenant 白名单未配置 `scopes` 时，`api_clients.allowed_scopes` 默认取全集；每次签发必须把 client 当时的权限复制到 `api_access_tokens.scopes`，Token 生命周期内不做动态继承。
- 生产协议的初始来源注册表仅包含 `zhaopin`；Fake Adapter 只用于内部测试，不进入公共来源枚举或生产表约束。增加来源需要同步发布 contracts、Agent Adapter 和 migration。
- `externalRequestId` 由调用方生成，推荐 UUIDv7、ULID 或带业务前缀的 ULID，字符规则为 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$`，在 `client_id` 内唯一；网络重试必须复用。同一 ID 搭配不同 payload 返回 409。
- `request_payload` 是创建时生成的版本化、规范化 JSONB 快照，只包含 `schemaVersion`、`source`、`queries`、`collectionMode`、`metadata`，不保存 externalRequestId、tenant、client、任何 Token、Idempotency-Key 或 callback 配置。callback URL、Token ciphertext、nonce、fingerprint 和 expiresAt 存入专用 Job 回调配置；幂等判定只使用本地 HMAC fingerprint，不解密 Token。
- `capabilities` 保存版本、支持来源、最大并发和浏览器能力；`availableSlots` 是心跳瞬时状态，不写入 capabilities，且必须满足 `0 <= availableSlots <= maxConcurrentTasks`。
- `browser_profiles` 只能由有效 Agent Session 的心跳写入，按 `(agent_id, source, profile_key)` upsert。`profile_key` 是不透明本机引用，禁止绝对路径、Cookie 或 Profile 内容；未上报项超过离线阈值后标记 `unavailable`，不立即删除。

以下估算基于 2 名后端、1 名 Agent/自动化工程师和 1 名测试兼运维工程师。

| 阶段 | 预计周期 | 主要工作 | 完成门槛 |
|---|---:|---|---|
| 0. 数据库环境基线 | 3-5 天 | 验证开发库连接/权限；冻结生产镜像、网络、数据卷、备份恢复与容量方案 | 开发准入记录和生产数据库部署评审通过 |
| 1. 工程与契约基线 | 3-5 天 | 锁定依赖、CI、Ruff/MyPy/Pytest、OpenAPI、Agent 协议、错误码、状态机 | 构建和静态检查通过；协议快照受版本控制 |
| 2. Schema 与持久化 | 1 周 | 开发隔离 schema 验证 Alembic；生产容器空库初始化；实现 Repository/UoW | 两种环境正向迁移、回滚和重复部署测试通过 |
| 3. 两类身份系统 | 1-1.5 周 | tenant allowlist、调用方 Token、provisioning CLI、设备认证、Agent Session | 未授信 tenant/Agent 无法获得 Token；两类 Token 不可互换 |
| 4. 作业控制面 | 1-1.5 周 | 幂等创建、任务拆分、查询、暂停、恢复、取消、事件 | 重复创建和并发控制产生确定结果 |
| 5. 调度与租约 | 1.5-2 周 | SKIP LOCKED、续租、fencing、attempt、过期恢复 | 双 Agent 只能有一个有效执行者；迟到写入被拒绝 |
| 6. Agent 通用运行时 | 1.5-2 周 | Supervisor、Transport、SQLite Spool、Profile 锁、子进程回收 | API/Agent 重启和断网后可恢复且不丢已解析记录 |
| 7. 结果与通知 | 1 周 | 幂等批量写入、游标分页、事件、Outbox、Webhook | 重复上传不重复落库；通知失败不阻塞任务事务 |
| 8. SourceAdapter | 1.5-2 周 | Fake Adapter、一个真实来源、登录检测、人工验证 | 先通过 Fake E2E，再通过两台真实执行机验收 |
| 9. 上线准备 | 1 周 | 压测、故障演练、安全检查、运行手册和回滚计划 | 全部生产门槛通过后发布镜像和 Agent 安装包 |

完整生产化预计 10-12 周。人员减少时延长周期，不改变阶段依赖关系。

### 5.2 Webhook 集成与凭证边界

调用方访问控制中心和控制中心访问上游回调 API 是两个独立认证方向。前者继续使用
tenantKey 换取的 Caller Token；后者使用上游为每个 Job 单独签发的短期 Callback
Token。Callback Token 只能访问该 Job 登记的 callback URL，不能调用控制中心、不能
访问其他 Job，也不能替代 Caller Token。

创建 Job 的 `callback` 对象冻结为 `url`、`accessToken`、`tokenType=Bearer` 和可空
`expiresAt`。callback 可整体为 null；非空时前三项必须同时存在。accessToken 长度为
43-1024，调用方应使用至少 32 字节密码学安全随机源生成；Token 必须位于创建 Job 的
JSON body，禁止进入 URL/query、metadata 和日志。控制面 API 的生产入口仍应使用 HTTPS。
expiresAt 省略时，控制面按首次成功创建
时间加 604800 秒计算；显式值必须带时区、晚于当前时间且不超过七天上限。202 响应
返回权威 `callbackTokenExpiresAt`；无 callback 时该固定字段返回 null，幂等重放不得延长
该时间。Callback Token 不得与当前 Caller Token 相同，禁止跨身份域复用凭证。

tenant 白名单为每个调用方配置 `callbackAllowlist`，值为规范化 HTTP 或 HTTPS 完整 URL。
创建 Job 时 callback URL 必须精确命中当前 tenant 的列表，并禁止 userinfo、fragment 和
3xx 重定向跟随，避免 URL 变体或重定向绕过目的地址限制。HTTP 用于受信任内网部署；
跨不受信任网络的回调必须由部署方配置 HTTPS。

控制面数据库保存 Callback Token ciphertext，不保存明文，也不与上游协调 digest
算法。0.4 固定使用独立 `CALLBACK_TOKEN_ENCRYPTION_KEY` 执行 AES-256-GCM，使用
96-bit 随机 nonce，AAD 包含 schema、callbackConfigId、collectionJobId 和
credentialVersion。独立 `CALLBACK_TOKEN_FINGERPRINT_KEY` 计算 HMAC-SHA256
fingerprint，仅用于 Job 幂等和 Token 轮换重放比较，不发送给上游、不用于回调鉴权。
上游负责自己的 Token hash、scope、Job/接口绑定、过期和吊销。

`PUT /v1/collection-jobs/{jobId}/callback-token` 用于七天到期前或 401/403 后轮换。
接口要求 Caller Token 具备 `collection:control` 和 Idempotency-Key，只能替换 Token 与
expiresAt，不能改变 URL。成功后更新 ciphertext、nonce、fingerprint、expiresAt，递增
credentialVersion，并唤醒 credential_blocked Outbox。Job 未终态或仍存在未成功
Outbox 时允许轮换；否则返回 409。

创建与轮换接口固定使用 401 表示 Caller Token 无效，403 表示 scope 不足，404 表示
未知或跨 tenant Job，409 表示幂等冲突或当前状态不可轮换，422 表示 callback URL、
Token、tokenType 或 expiresAt 不合法。

Job 状态变化时，领域事件和 Outbox 在同一事务创建；网络调用不进入状态事务。
`collection_job_callback_configs` 已由 `0004` 建立并保存 Job 级 URL 与当前 Token ciphertext；
`0005` 已将 `webhook_outbox` 改为只引用 event_id 和 callback_config_id，不复制 URL 或
凭证，唯一约束为 `(event_id, callback_config_id)`。无 callback 的 Job 仍生成事件，但不
生成 Outbox。`0006` 增加永久轮换幂等历史，并为 Outbox 增加 pending、
credential_blocked、delivered、permanently_failed 状态；轮换只恢复未投递的
credential_blocked 记录，不重新激活 delivered 或 permanently_failed 记录。`0007`
增加 dispatch claim UUID、领取时间和短租约到期时间；Dispatcher 使用
`FOR UPDATE SKIP LOCKED` 领取后先提交，再执行网络请求，并以 claim ID 比较并交换结算。
`0008` 增加 `credential_destroyed_at` 和 Callback 凭证材料完整性约束：可用状态要求
ciphertext/nonce 同时存在，销毁状态要求两者同时为 null。fingerprint、expiresAt、
credentialVersion、encryptionKeyVersion 和轮换幂等历史不属于可用凭证材料，继续保留
用于确定性重放和安全审计。

`0009` 增加重试耗尽后的调用方运维查询与受控重放。`GET
/v1/collection-jobs/{jobId}/webhook-deliveries` 要求 `collection:read`，默认查询
permanently_failed，并以绑定 jobId/status 的 createdAt + outboxId 游标分页；响应不包含 URL
或凭证材料。`POST .../{outboxId}/replay` 要求 `collection:control` 和 Idempotency-Key，
只接受 `last_failure_code` 为 `retry_exhausted_*`、无 claim、Callback 凭证仍可用且未过期的
permanently_failed 记录。成功后 Outbox 恢复 pending、当前轮 attempt_count 归零、
replay_count 递增并立即可调度；此前次数和失败码追加到 webhook_outbox_replays 永久历史。
原始 Idempotency-Key 不落库，相同键在后续状态变化后仍返回首次结果。未知、跨 Job 和跨
tenant 资源统一 404；凭证销毁、过期或状态不符合资格统一 409。锁序固定为 Outbox 后
callback config，与 Dispatcher 结算一致。

Webhook 使用 `Authorization: Bearer <Callback Token>` 和
`Idempotency-Key: <eventId>`。0.4 事件类型冻结为 `collection.job.accepted`、
`collection.job.desired_state_changed`、`collection.job.terminal`。任意 2xx 表示成功；
连接失败、超时、408、425、429、5xx 有界重试；401/403 暂停并等待轮换；其他 4xx
永久失败；3xx 不跟随。上游必须按 eventId 去重，并用 jobVersion 处理迟到事件。

Dispatcher HTTP 客户端不读取响应正文，关闭环境代理并设置请求超时。默认并发 4、
claim 租约 30 秒、HTTP 超时 10 秒、最多 8 次实际请求，指数退避从 5 秒开始且最大
900 秒；429/503 的 Retry-After 同样受上限约束。进程在请求期间退出时，claim 到期后
可由其他 worker 接管，因此仍是至少一次投递。401/403 结算必须复核 credentialVersion，
避免旧 Token 的迟到响应覆盖并发轮换后恢复的 pending 状态。

控制面通过不进入 OpenAPI 的 `GET /internal/metrics` 暴露 Webhook 聚合指标。数据库实时
快照提供 pending 数量、最老 pending 年龄、credential_blocked、permanently_failed、
retry_exhausted 和活动 claim；进程内 Counter/Histogram 提供 delivered/retry/blocked/
failed 速率、投递耗时、过期 claim 接管、陈旧结算和 Dispatcher 异常。指标不使用 URL、
tenant、Job、Event、Outbox 或失败文本作为 label。该端点不使用 tenant Caller Token，生产
必须由反向代理或网络策略仅允许监控网络访问；多 API 副本必须逐实例抓取进程内 Counter。
每次结算同时输出单行 JSON 安全审计，仅包含 Event/Job/Outbox ID、credentialVersion、
attempt、结果和受控失败码，不记录 URL、Authorization、Token、响应正文或底层异常文本。
受控重放的数据库历史只记录 Outbox ID、操作键摘要、重放序号、此前 attempt 数与稳定失败
分类，不记录 URL、Token、fingerprint、ciphertext、nonce 或网络异常原文。

“一次性”表示每个 Job 单独签发，而不是首次 HTTP 调用后立即失效。响应丢失时控制面
必须能以同一 Token 和 eventId 重试。终态事件及全部 Outbox 成功后控制面销毁
ciphertext/nonce，并记录销毁时间；该判断在 callback 配置行锁内与最后一个 delivered
结算原子完成，避免并发投递漏销毁。销毁后拒绝新的轮换，但既有成功轮换的幂等重放
仍返回首次结果，且不会恢复密文。上游可立即吊销 Token。0.4 不再使用
`WEBHOOK_SIGNING_KEY` 参与回调认证。

## 6. 第一条纵向链路

以下 FakeSourceAdapter 自动化闭环已经实现：

```text
tenantKey 换调用方 Token
  -> 创建作业并拆分任务
  -> 受控 provisioning 预置 Agent
  -> Agent 设备凭证换 Session
  -> Agent 心跳并领取能力匹配的任务
  -> 确定性 Adapter 分页生成并幂等上传 ParsedJobRecord
  -> 逐页 checkpoint 并沿用服务端最新 leaseExpiresAt
  -> 完成任务并汇总作业
  -> 调用方分页查询结果
```

该闭环通过真实 FastAPI 调用方/Agent 路由和真实 PostgreSQL 完成，并验证批次重放、
Job 终态聚合、结果 keyset pagination、Agent 载荷无 tenant/callback 信息及全表事务
回滚。它使用测试 Actor，不代表桌面 Runtime 已在真实执行机运行。当前桌面 Agent 已
实现 Transport、SQLite Spool、Supervisor、Profile 锁、PID 复用安全的浏览器进程树
回收和单任务循环的本地闭环；双 Agent
领取和租约接管由控制面专项集成测试覆盖，真实双执行机仍在 8.4 验收。真实来源 Adapter
与控制面 Fake 闭环保持分层验证，避免把网站页面故障误判为
API、租约或持久化缺陷。

`ZhaopinSourceAdapter` 已按共享接口迁移旧采集器中可复用的 URL、列表/详情选择器、
字段优先级、岗位 ID 提取和持久化浏览器配置，并通过不访问外网的 DOM 驱动测试。
Adapter 只输出 `ParsedJobRecord`，不导入旧 PlatformClient、上游任务模型或数据库代码；
岗位导航限制在 `zhaopin.com` HTTPS 地址。默认城市表只包含已有实现可证实的北京
`530`，新地区必须先验证编码再显式发布。该完成项不包含登录/安全验证人工继续或真实
网站选择器验收，二者仍按 8.3、8.4 单独交付。

任务 8.3 的实现边界已冻结：Adapter 检测已验证的智联登录信号
（`passport.zhaopin.com`、路径段 `/login`、登录提示）和安全验证信号
（标题 `Security Verification`、`TEOCaptchaWidget`、`正在验证连接安全性`）。
无头模式遇到这些页面立即返回稳定错误；可见模式保持同一个 WebDriver 和同一个
持久化 Profile，向本地运行时报告 `login_required`、`verification_required`、
`resumed`，在有界超时内轮询页面状态。轮询每次检查暂停/取消意图；不启动第二个
浏览器，不上传 Cookie、Profile 路径、页面源码、截图或验证码内容。该切片只通过
离线 Selenium 兼容 DOM 测试，不代表真实网站登录、验证或双执行机验收；后者属于
8.4。

Adapter 控制安全点统一为页开始和下一条记录开始前。详情导航一旦开始，当前岗位必须
完成解析并交付，不能因随后观察到的暂停/取消而丢弃已开始的记录。页级 checkpoint
仍要求 Agent Runtime 已确认该页全部记录；在 SQLite spool/Transport 完成前，Adapter
内部的 `yield` 不应被误解为 API 已接受。

## 7. 必测场景

- provisioning CLI 重复执行同一安装请求时不得创建多个身份。
- Session 接口收到未知 agentId 时返回认证失败且不产生数据库行。
- Agent disabled 或凭证轮换后，旧 Session 立即不可用。
- 两个 Agent 同时领取任务时只有一个获得有效租约。
- Agent A 过期、Agent B 接管后，Agent A 的迟到上传返回冲突。
- Agent 上传成功但未收到确认时，重复批次不产生重复岗位记录。
- 暂停、取消、完成及租约到期并发发生时状态转换保持确定。
- 开发数据库或生产 PostgreSQL 容器短时不可用时，API fail-fast/降级且恢复后不破坏租约一致性。
- tenant 从白名单移除后，未过期调用方 Token 也必须失效。
- 日志扫描不得出现数据库密码、tenantKey、deviceSecret 和任何明文 Token。
- Callback 真实 HTTP 链路必须覆盖 503/Retry-After、服务端断连、读超时、3xx 不跟随、401 后 Token 轮换恢复，以及 claim 过期接管后的迟到结算 fencing。
- Callback 故障注入后的 Outbox 只能保存稳定失败分类；应用日志及 httpx/httpcore 依赖日志不得出现 Callback URL、Bearer Token 或上游响应正文。
- 两个真实 Agent 节点完成登录接管、Profile 重启复用和失联接管。

## 8. 上线门槛

1. 生产 PostgreSQL 使用固定的 `18.1-alpine` 基线镜像，不发布 5432，并将持久卷挂载到 `/var/lib/postgresql`；升级补丁版本前单独验证和备份。
2. 生产 migration 已在同版本临时容器验证；开发 migration 已在隔离 schema 验证。
3. API 数据库账号不具备实例管理、超级用户或非目标 schema 的权限。
4. 数据库密码仅由 Secret 注入；已粘贴或临时共享的密码在上线前完成轮换。
5. `collection_agents` 只能由受控 CLI 创建，Session API 无注册旁路。
6. Agent 任务载荷不存在 tenantId、clientId、callback 和调用方 metadata。
7. PostgreSQL 中不存在 HTML、截图、Cookie、Profile 内容和业务派生结果。
8. 双 Agent 抢占、fencing、断网补传和 API/数据库容器重启测试全部通过。
9. 生产 PostgreSQL 备份恢复和数据卷故障处理完成演练。
10. 形成 API 部署、Agent 安装、凭证轮换、数据库 migration 和故障处理手册。

## 9. 实施产物

- `docs/job-collection-engine-design.html`：0.4 技术方案和系统边界。
- `docs/implementation-plan.md`：本实施计划和阶段门槛。
- `deploy/docker/docker-compose.dev.yml`：API 与 migration，连接既有开发数据库。
- `deploy/docker/docker-compose.prod.yml`：API、migration、生产 PostgreSQL 和持久卷。
- `deploy/config/tenant-allowlist.example.yml`：调用方授信配置示例。
- `deploy/examples/`：API 和至少两个 Agent 节点的配置示例。
- `services/crawler_api/.../migrations/`：应用 schema 版本。
- `services/crawler_api/ops/`：Agent 预置、轮换和禁用工具。
- `contracts/`：调用方、Agent、岗位记录和事件协议。
