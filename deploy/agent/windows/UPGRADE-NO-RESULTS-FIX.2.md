# Windows Desktop Agent no-results-fix.2 升级清单

适用环境：Windows x64、CPython 3.12，已安装 `desktop-collection-agent` 0.2.0。
此补丁修复 `no-results-fix.1` 未覆盖的列表等待超时：当智联招聘页面既无可解析职位
列表，也无可识别空结果标记时，45 秒等待结束后 Agent 将任务置为 `failed`，失败码为
`listing_unavailable`。明确的空结果仍使用 `no_results`。两种失败均经 API 的
`complete` 接口原子释放租约和浏览器 Profile。不能把页面加载失败断定为“确实无职位”，
因此两个失败码保持区分。补丁只替换 Agent wheel，不改凭据、环境文件、计划任务、
Profile 或本地 spool；wheel 版本号仍为 0.2.0，安装时必须强制重装。

以下以 Node 01 为例，在运行该计划任务的 Windows 用户会话执行。Node 02 改用自己的
配置及日志路径。保留升级前可信 wheel 供回退。

## 1. 校验补丁

将 ZIP 和同名 `.zip.sha256` 文件传到 `C:\JobCollectionAgent\staging`，先对照可信发布
记录核对 ZIP 哈希，然后执行：

```powershell
$PatchZip = 'C:\JobCollectionAgent\staging\desktop-agent-windows-x86_64-0.2.0-no-results-fix.2.zip'
$PatchRoot = 'C:\JobCollectionAgent\staging\no-results-fix.2'
$ExpectedHash = ((Get-Content -LiteralPath "$PatchZip.sha256" -Raw).Trim() -split '\s+')[0]
$ActualHash = (Get-FileHash -LiteralPath $PatchZip -Algorithm SHA256).Hash.ToLower()
if ($ActualHash -ne $ExpectedHash) { throw "ZIP SHA-256 mismatch: $ActualHash" }
Expand-Archive -LiteralPath $PatchZip -DestinationPath $PatchRoot -Force
Get-Content -LiteralPath (Join-Path $PatchRoot 'SHA256SUMS') | ForEach-Object {
    if ($_ -match '^([0-9a-f]{64})\s+(.+)$') {
        $Path = Join-Path $PatchRoot $Matches[2]
        $Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
        if ($Hash -ne $Matches[1]) { throw "member SHA-256 mismatch: $Path" }
    } else { throw "invalid SHA256SUMS line: $_" }
}
Write-Output 'patch checksums verified'
```

## 2. 停止并安装

`stop.ps1` 请求 Agent 协作退出。若 180 秒超时并强制停止，它会返回错误；此时先检查
遗留进程和服务端租约恢复，避免同时运行两个 Agent。不要删除未经确认的 Profile 锁。

```powershell
$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Config = 'C:\JobCollectionAgent\release\agent-node-01.env'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\JobCollectionAgent\release\agent\windows\stop.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Agent did not stop cleanly; inspect process and lease state' }
if ((Get-ScheduledTask -TaskName 'Job Collection Desktop Agent').State -eq 'Running') { throw 'Agent is still running' }
& $Python -m pip install --no-index --no-deps --force-reinstall (Join-Path $PatchRoot 'wheelhouse\desktop_collection_agent-0.2.0-py3-none-any.whl')
if ($LASTEXITCODE -ne 0) { throw 'desktop-collection-agent install failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'dependency check failed' }
```

## 3. 验证并启动

```powershell
& $Python -c @'
import inspect
from desktop_agent.runtime.supervisor import AgentSupervisor
from desktop_agent.source_adapters.zhaopin import ZhaopinPageLoadError

source = inspect.getsource(AgentSupervisor._run_lease)
assert 'ZhaopinPageLoadError' in source
assert 'listing_unavailable' in source
assert 'outcome="failed"' in source
print('no-results-fix.2 code verified')
'@
if ($LASTEXITCODE -ne 0) { throw 'patch code verification failed' }
& 'C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe' --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Agent config check failed' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\JobCollectionAgent\release\agent\windows\start.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Agent start failed' }
Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'Job Collection Desktop Agent' | Format-List LastRunTime, LastTaskResult
```

## 4. 核对目标任务

如果任务 `721b14a3-3e41-423f-8d80-e45edcb0240e` 被重新领取，等待本轮列表
加载窗口结束，然后查看日志：

```powershell
$TaskId = '721b14a3-3e41-423f-8d80-e45edcb0240e'
$Log = 'C:\JobCollectionAgent\release\data-node-01\logs\desktop-agent.log'
Select-String -LiteralPath $Log -Pattern $TaskId | Select-Object -Last 20
```

预期出现 `task source failed` 和 `failure_code=listing_unavailable`，此后 Agent 应可领取
其他任务。还需在服务端按任务 ID 核对 `status=failed`、`failure_code=listing_unavailable`、
`leased_agent_id`、`leased_profile_id`、`lease_token_hash`、`lease_expires_at` 均为空，
关联 Profile 为 `ready`。本包只改变升级后的执行行为；安装动作本身不会直接修改旧租约。

## 5. 回退

停止 Agent，使用保留的上一版可信 wheel 按第 2 节的 `pip install --no-index --no-deps
--force-reinstall` 方式重装，执行配置检查后启动。不要删除 Profile 或 spool。前后 wheel
版本号相同，应凭 wheel 哈希及实际代码确认回退。
