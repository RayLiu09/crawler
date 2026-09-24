# 8.4 桌面 Agent 与双执行机验收

本文档是任务 8.4 的受控验收手册。它不把控制面集成测试 Actor、离线 DOM
FakeDriver 或单元测试当作真实桌面 Agent 证据。

## 当前准入结论

截至当前版本，8.4 的真实执行仍未开始，原因是：

- Agent Runtime 的本地实现已经接入入口，具备 Supervisor、HTTP Transport、SQLite
  spool、Profile 锁、任务循环和基于 PID/创建时间的浏览器进程树监督；
- 但尚未提供两台已安装、可见浏览器和持久化 Profile 的执行机；
- Transport 重试、受监督浏览器子进程树和真实断网/崩溃恢复已有本地自动化覆盖，
  但仍未完成 Windows/Linux 执行机与真实 Chrome/Edge 的实机验收；
- 当前至少两个 Agent 的 PostgreSQL 测试只验证控制面 Session、heartbeat、claim、
  fencing 和 recovery，不启动桌面浏览器；
- 当前通过的 Zhaopin 测试使用离线 Selenium 兼容 FakeDriver，不访问真实智联页面。

因此本文件定义验收入口和证据要求，但不宣称 8.4 已通过。

## 运行前置条件

### 控制中心

1. 部署已通过 migration、readiness 和密钥注入检查的 `crawler-api`。
2. PostgreSQL 已启用备份、监控和审计；测试使用独立租户测试数据，不使用生产岗位数据。
3. 使用受控 provisioning CLI 为两个执行机预置不同的 `agentId` 和 `deviceSecret`。
4. 两个 Agent 的 `agentId`、Profile 根目录和本地数据目录完全不同。
5. 两个 Agent 均能访问控制面 Agent API；控制面不接收入站浏览器连接。

### 执行机 Node 01 / Node 02

每台执行机必须具备：

- 可见 Chrome/Edge 和可写的持久化 Profile 根目录；
- 独立的操作系统凭证存储，不把 `deviceSecret` 写入仓库或普通日志；
- Windows 节点以专用账号的 Interactive 登录计划任务运行，Linux 节点以同一账号的
  systemd user service + desktop autostart 运行；不得替换为无交互桌面的系统服务；
- 独立的 `AGENT_DATA_DIR`，其中只允许保存解析记录 spool、检查点和运行状态；
- 可观测的 Agent 进程退出码、结构化状态事件和本地锁状态；
- 真实人工可以看到并操作该节点的浏览器窗口。

## 验收场景

| 编号 | 场景 | 必须观察到的结果 | 证据 |
|---|---|---|---|
| A01 | 两节点启动和 Session 建立 | 两个不同 Agent 均成功换取 Session；互换 deviceSecret 或 Session 必须 401 | 脱敏启动日志、API 审计、`agent_sessions` 行 |
| A02 | Profile 首次创建 | Node 01 和 Node 02 各自只创建本机 Profile；控制面只收到 `profileKey`、状态和能力，不收到路径/Cookie | 心跳 payload 摘要、`browser_profiles` 行 |
| A03 | 同节点 Profile 锁 | 同一 `(source, profileKey)` 第二个本地任务不能同时打开 Profile；第一个锁释放后第二个才能获得 | 本地锁事件、浏览器 PID、无并发窗口证据 |
| A04 | Profile 重启复用 | 关闭 Agent 进程后重启，使用同一 Profile 继续；不重新要求登录（除非来源实际使登录态失效） | 重启前后 Profile 标识、页面登录状态、无 Cookie 导出 |
| A05 | 人工登录/安全验证 | 可见窗口进入 `login_required` 或 `verification_required`；人工在同一窗口完成后回到采集流程；不创建第二个 driver | 状态事件顺序、driver 数量、原窗口 PID、页面结果 |
| A06 | 运行中暂停 | 控制面发 pause；Agent 在安全点停止新导航、提交已接受记录、保存检查点并释放租约 | Job/Task 状态、checkpoint、attempt、Profile 状态 |
| A07 | 运行中取消 | 控制面发 cancel；Agent 停止新导航，已开始记录按协议处理，最终 Task 为 cancelled | Job/Task 终态、无后续记录上传 |
| A08 | 双节点任务分配 | 两节点不会领取同一活动 Task；每个 Agent 只使用自己上报的 Profile | claim 响应、Task lease、Agent/Profile 绑定 |
| A09 | Node 01 失联 | Node 01 停止心跳并超过离线/租约阈值；Node 02 可接管，旧 fencing/lease 写入被 409 拒绝 | 时间线、attempt、fencingToken、迟到请求响应 |
| A10 | spool 崩溃恢复 | API 断开或 Agent 进程崩溃后，未确认 ParsedJobRecord 从 SQLite spool 重放；同 batch 重放不重复落库 | spool 行、batch 回执、数据库去重结果 |
| A11 | 敏感信息边界 | 控制面、日志、spool 和回调事件均没有 Cookie、页面源码、截图、Profile 内容、deviceSecret 或 Callback Token | 脱敏日志扫描、数据库字段审计、spool 内容检查 |
| A12 | 完成与资源释放 | 任务完成后浏览器、Profile 锁、租约和 Agent current task 均释放；Job 汇总为终态 | 进程树、锁文件、Task/Agent/Profile 行 |

## 执行顺序

1. 在测试 tenant 下提交一个两页以上的 Zhaopin 作业，确认两个节点均 heartbeat
   且 Profile 状态为 `ready`。
2. 先执行 A01-A04，确认身份、Profile 隔离和重启复用，再执行真实来源页面检查。
3. 在列表页或详情页触发登录/安全验证，执行 A05；必须记录同一浏览器窗口证据。
4. 分别执行 A06、A07，确认控制命令只在 Adapter 安全点生效。
5. 使用两个可领取 Task 执行 A08；不得通过固定分配或人工挑选 Task 代替调度验证。
6. 在 Node 01 持有租约时停止其网络或进程，等待租约过期后执行 A09。
7. 在记录上传前断开 API 或终止 Agent，恢复后执行 A10。
8. 最后执行 A11、A12，并导出脱敏数据库快照和运行日志索引。

## 通过门槛

只有以下条件全部满足，任务 8.4 才能标记完成：

- 两台不同执行机各自完成至少一次真实智联采集；
- 两台执行机都完成首次人工登录或安全验证，以及 Profile 重启复用；
- 至少一次暂停、取消、租约接管和旧 fencing 拒绝通过；
- 断网/崩溃后的 spool 重放和批次幂等通过；
- A11 敏感信息扫描无命中；
- 所有结果均有可复核时间线、API 响应、数据库快照和脱敏日志证据。

控制面现有 PostgreSQL 集成测试可作为 A08/A09 的协议级先决检查，但不能替代上述
真实 Agent 验收。若执行环境未满足 Runtime、浏览器或双节点前置条件，执行脚本必须以
明确的 prerequisite failure 退出，不得自动降级为 FakeDriver 或测试 Actor 并输出
“验收通过”。
