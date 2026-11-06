# Desktop Agent 0.2.0 resume-page100-fix.2 升级清单

此补丁包含 `crawler-contracts` 和 `desktop-collection-agent` 两个 wheel，修复 Windows
Profile 锁的 PID 判活、强制停止后从已确认页继续采集，并将单个 query 的最大页数提高到
100。wheel 版本号仍为 `0.2.0`，因此升级命令必须使用 `--force-reinstall`。

补丁源代码提交：`d662c08948e74b55dca07c01f6d568ab75e83c31`。目标环境为 Windows
x64、CPython 3.12。补丁不替换 Agent ID、设备凭据、环境文件、计划任务、浏览器
Profile 或 SQLite spool。

## 1. 先升级 crawler-api

当前 `10.100.11.51:8080` 的 OpenAPI 仍显示 `TaskLease.pageLimit` 最大为 30，且
`TaskLease` 缺少 `processedCount`。先用现有生产发布流程将 crawler-api 升级到包含上述
提交的镜像；此变更不需要数据库迁移。API 和 Agent 的 Python 包版本号同为 `0.2.0`，
不能仅凭版本号判断是否已经包含补丁。部署后，在将要升级的 Windows 执行机上运行：

如果 API 使用仓库中的生产 Compose 文件，可在构建机从该提交构建镜像，并将导出的 tar
交付到 API 主机。`/path/to/crawler` 和现有的生产环境变量注入方式按实际部署替换；
不要将密码写进命令行或提交到仓库：

```bash
cd /path/to/crawler
git fetch origin main
git switch --detach d662c08948e74b55dca07c01f6d568ab75e83c31
docker build -f services/crawler_api/Dockerfile \
  -t job-collection-engine/crawler-api:0.2.0-resume-page100-fix.2 .
docker save -o crawler-api-0.2.0-resume-page100-fix.2.tar \
  job-collection-engine/crawler-api:0.2.0-resume-page100-fix.2
sha256sum crawler-api-0.2.0-resume-page100-fix.2.tar
```

在 API 主机校验 tar 的 SHA-256 后，按当前生产环境变量配置执行：

```bash
docker load -i crawler-api-0.2.0-resume-page100-fix.2.tar
CRAWLER_API_IMAGE=job-collection-engine/crawler-api:0.2.0-resume-page100-fix.2 \
  docker compose -f deploy/docker/docker-compose.prod.yml up -d --no-deps crawler-api
curl --fail --silent http://10.100.11.51:8080/health/ready
```

多副本环境需要先更新所有 API 副本，然后再升级 Agent。API 构建/部署由现有生产
发布流程执行；本 ZIP 只包含 Windows Agent wheel，不包含 API 镜像 tar。

在将要升级的 Windows 执行机上验证新 API 合同：

```powershell
$Config = 'C:\JobCollectionAgent\release\agent-node-01.env'
$ApiUrl = ((Get-Content -LiteralPath $Config | Where-Object { $_ -match '^CRAWLER_API_URL=' } | Select-Object -First 1) -replace '^CRAWLER_API_URL=', '').TrimEnd('/')
if (-not $ApiUrl) { throw 'CRAWLER_API_URL is missing from Agent config' }
$Schema = Invoke-RestMethod -Uri "$ApiUrl/openapi.json" -TimeoutSec 15
$Lease = $Schema.components.schemas.TaskLease
$Query = $Schema.components.schemas.CollectionQuery
if ($null -eq $Lease.properties.PSObject.Properties['processedCount']) { throw 'crawler-api does not expose TaskLease.processedCount' }
if ([int]$Lease.properties.pageLimit.maximum -ne 100 -or [int]$Query.properties.pageLimit.maximum -ne 100) { throw 'crawler-api does not accept pageLimit=100' }
Write-Output 'crawler-api contract is ready for this Agent patch'
```

只有输出 `crawler-api contract is ready for this Agent patch` 才继续。Node 02 将
`$Config` 改为实际的配置文件路径。

## 2. 校验并解压补丁

以下命令以 Node 01 为例，必须在拥有计划任务和设备凭据的交互式 Windows 用户会话中
执行。不要同时启动第二个前台 Agent。

```powershell
$PatchZip = 'C:\JobCollectionAgent\staging\desktop-agent-windows-x86_64-0.2.0-resume-page100-fix.2.zip'
$PatchRoot = 'C:\JobCollectionAgent\staging\resume-page100-fix.2'
$ExpectedHash = ((Get-Content -LiteralPath "$PatchZip.sha256" -Raw).Trim() -split '\s+')[0]
$ActualHash = (Get-FileHash -LiteralPath $PatchZip -Algorithm SHA256).Hash.ToLower()
if ($ActualHash -ne $ExpectedHash) { throw "patch ZIP SHA-256 mismatch: $ActualHash" }
Expand-Archive -LiteralPath $PatchZip -DestinationPath $PatchRoot -Force
Get-Content -LiteralPath (Join-Path $PatchRoot 'SHA256SUMS') | ForEach-Object {
    if ($_ -match '^([0-9a-f]{64})\s+(.+)$') {
        $Path = Join-Path $PatchRoot ($Matches[2] -replace '^\./', '')
        $Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
        if ($Hash -ne $Matches[1]) { throw "patch member SHA-256 mismatch: $Path" }
    }
}
Write-Output 'patch checksums verified'
```

外层 `.sha256` 文件必须从可信发布渠道取得并与发布记录核对。

## 3. 停止并升级 Agent

先确认上一轮任务已通过 checkpoint 安全退出。`stop.ps1` 超时后会强制停止计划任务并
返回错误；若发生这种情况，先按现有 RUNBOOK 检查遗留 Agent、浏览器进程和 Profile
锁，再继续升级。不要直接删除未确认的锁文件。

```powershell
$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Config = 'C:\JobCollectionAgent\release\agent-node-01.env'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\stop.ps1
if ((Get-ScheduledTask -TaskName 'Job Collection Desktop Agent').State -eq 'Running') { throw 'Agent is still running' }

& $Python -m pip install --no-index --no-deps --force-reinstall (Join-Path $PatchRoot 'wheelhouse\crawler_contracts-0.2.0-py3-none-any.whl')
if ($LASTEXITCODE -ne 0) { throw 'crawler-contracts install failed' }
& $Python -m pip install --no-index --no-deps --force-reinstall (Join-Path $PatchRoot 'wheelhouse\desktop_collection_agent-0.2.0-py3-none-any.whl')
if ($LASTEXITCODE -ne 0) { throw 'desktop-collection-agent install failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependency check failed' }
```

如果 `$Config` 中显式配置了 `ZHAOPIN_MAX_PAGE_LIMIT=50`，将它改为
`ZHAOPIN_MAX_PAGE_LIMIT=100`；若未配置，该补丁默认值已是 100。不要修改 Agent ID、
设备凭据、`AGENT_DATA_DIR` 或 `BROWSER_PROFILE_ROOT`。

```powershell
& $Python -c @'
from crawler_contracts.agent_protocol import TaskLease
from crawler_contracts.collection_jobs import CollectionQuery
from desktop_agent.source_adapters.zhaopin.adapter import DEFAULT_MAX_PAGE_LIMIT

assert CollectionQuery(keyword="test", page_limit=100).page_limit == 100
assert "processed_count" in TaskLease.model_fields
assert DEFAULT_MAX_PAGE_LIMIT == 100
print("Agent patch code verified")
'@
if ($LASTEXITCODE -ne 0) { throw 'Agent patch code verification failed' }
& 'C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe' --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Agent config check failed' }
```

## 4. 启动和观察

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\start.ps1
Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName 'Job Collection Desktop Agent' | Format-List LastRunTime,LastTaskResult
Get-Content -LiteralPath 'C:\JobCollectionAgent\release\data-node-01\logs\desktop-agent.log' -Tail 80
```

预期计划任务为 `Running`，运行中的 `LastTaskResult` 为 `267009` (`0x41301`)。
对于有历史 checkpoint 的 Task，日志中的 `task lease acquired` 应显示已保存的
`page_checkpoint`；下一次 `zhaopin listing page loaded` 应从后一页开始，随后出现
`task checkpoint acknowledged` 或 `task execution completed`。同时确认 crawler-api
中的 Job 状态和 `processedCount` 持续推进。
