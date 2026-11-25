# Desktop Agent 0.5.0 升级说明

本版本将 crawler-api、crawler-contracts、desktop-collection-agent 和工作区版本统一为
`0.5.0`，补齐智联招聘右侧详情的岗位所在地、招聘人数和发布时间。

## 数据变化

- `locationText`：岗位所在地原文，例如 `湛江·霞山区`；与 `addressText` 的详细工作
  地址分开保存。任务 `region` 仍只表示搜索条件，不能代替岗位实际所在地。
- `hiringText`：保留 `招3人` 等原文；新增 `hiringCount`，仅明确的单一人数转为整数。
  `若干`、范围和未提供保持无数值，不按 0 或 1 人统计。
- `publishTimeText`：保留 `昨日发布` 等原文；新增 `publishedDate`、`publishedAt`。
  相对时间以 `collectedAt` 对应的北京时间换算日期，小时/分钟表达式所得日期为推算值，
  可由原文识别。仅来源明确提供时刻时填写带 `+08:00` 时区的 `publishedAt`。
  日期文本不补造零点时刻，更新、刷新、招聘人员回复时间不作为发布时间。
- 摘要优先按地址、经验、学历、人数、时间图标识别，不依赖标签下标；缺失字段按标签
  文本补充。列表所在地组合全部地区节点。详情提供岗位 ID 时，等待面板切换到目标 ID
  后再读取；缺少 ID 时使用新版详情标题核对。

新字段没有值时在 JSON 中省略（模型内为 `None`），旧字段的序列化规则不变，以保留
0.4.0 的记录哈希、批次请求哈希和本地 spool 重放。旧数据读取时不自动回填新指标。
Pydantic 最低版本提升至 2.12，以支持缺失新字段的统一序列化规则。

本次不新增数据库迁移，岗位字段继续存储在 `parsed_fields` JSONB 中。需要地区、人数
或日期索引的查询优化应另行评估。

## 升级顺序

1. 先部署同一发布源码构建的 `job-collection-engine/crawler-api:0.5.0`。
2. 检查 `/openapi.json` 的 `info.version` 为 `0.5.0`，并确认新增岗位字段可见。
3. 协作停止 Agent，备份配置、凭据、SQLite spool 和 Browser Profile。
4. 将 contracts 和 Agent 一起升级，再运行配置预检并启动。
5. 使用真实登录态执行一次受控任务，对照页面核验三个指标。

0.5 API 支持 0.4 Agent 的既有记录和批次；0.4 API 严格拒绝新字段，因此不能先升级
Agent。消费岗位结果的调用方也应兼容新增的可选字段。

先校验发布包外层 `.zip.sha256`，解压后按 `SHA256SUMS` 校验包内文件。以下命令由
运行 Agent 的专用桌面账号执行，路径按实际安装位置调整：

```powershell
$PatchRoot = 'C:\JobCollectionAgent\staging\0.5.0'
$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Config = 'C:\JobCollectionAgent\release\agent-node-01.env'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\stop.ps1
if ($LASTEXITCODE -ne 0) { throw 'Agent stop failed' }
& $Python -m pip install --no-index --find-links (Join-Path $PatchRoot 'wheelhouse') crawler-contracts==0.5.0 desktop-collection-agent==0.5.0
if ($LASTEXITCODE -ne 0) { throw 'Agent package upgrade failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
& $Python -c "from importlib.metadata import version; assert version('crawler-contracts') == '0.5.0'; assert version('desktop-collection-agent') == '0.5.0'; print('Package versions verified')"
if ($LASTEXITCODE -ne 0) { throw 'Package version check failed' }
& 'C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe' --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Agent configuration check failed' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\start.ps1
if ($LASTEXITCODE -ne 0) { throw 'Agent start failed' }
```

Linux 执行机同样先停止、备份，再安装匹配的两个 0.5.0 wheel，并按 RUNBOOK 执行
配置预检和启动。Windows 离线包不能作为 Linux 依赖包使用。

## 验收与回退

- 示例页面应得到 `locationText=湛江·霞山区`、`hiringText=招3人`、`hiringCount=3`、
  `publishTimeText=昨日发布`；`publishedDate` 为采集北京时间前一天，`publishedAt`
  不应出现。详细地址保留在 `addressText`。
- 核验人数、时间缺失或标签顺序变化的岗位；公司规模和回复时间不得混入这两个指标。
- 新版不保证来源每条岗位都提供三个指标，不能用搜索地区或采集时间填补缺失来源值。
- 新版 API、spool 和存储中一旦出现带新字段的数据，旧版程序不能直接读取它们。
  回退 Agent 前应先用 0.5.0 排空待上传队列；API 继续保留 0.5.0。若必须回退 API，
  需先制定新字段数据的导出和兼容迁移方案，不能直接降级或删除数据、spool、Profile。
