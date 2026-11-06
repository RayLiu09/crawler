# Windows Desktop Agent no-results-fix.1 升级清单

适用环境：Windows x64、CPython 3.12，现有 `desktop-collection-agent` 0.2.0 安装。
补丁只包含 Agent wheel；当前 API 的 `complete(outcome="failed")` 已支持把任务置为失败并
在同一事务中释放租约及浏览器 Profile。搜索首页无职位时，新 Agent 上报失败码
`no_results`；已采集职位后遇到空页仍正常结束。wheel 版本号仍为 0.2.0，必须强制重装。
补丁不替换 Agent ID、设备凭据、环境文件、计划任务、浏览器 Profile 或本地 spool。

以下以 Node 01 为例，在拥有计划任务的交互式 Windows 用户会话执行。Node 02 应换成
自己的配置及日志路径。升级前保留当前已安装版本对应的可信 wheel，以供回退。

## 1. 传输和校验

将 ZIP 和同名 `.zip.sha256` 文件传至 `C:\JobCollectionAgent\staging`，先从可信发布
记录核对外层 SHA-256，然后执行：

```powershell
$PatchZip = 'C:\JobCollectionAgent\staging\desktop-agent-windows-x86_64-0.2.0-no-results-fix.1.zip'
$PatchRoot = 'C:\JobCollectionAgent\staging\no-results-fix.1'
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
Write-Output 'Agent patch checksums verified'
```

## 2. 停止并安装

`stop.ps1` 会请求 Agent 协作退出。若等待 180 秒后超时并强制结束计划任务，脚本会返回
错误；此时先检查遗留 Agent/浏览器进程、Profile 锁和服务端租约恢复状态，再继续安装。
不要手工删除未经确认的锁文件。该补丁不会自动改变已经在服务端落库的任务状态。

```powershell
$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Config = 'C:\JobCollectionAgent\release\agent-node-01.env'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\JobCollectionAgent\release\agent\windows\stop.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Agent did not stop cleanly; inspect task and lease state' }
if ((Get-ScheduledTask -TaskName 'Job Collection Desktop Agent').State -eq 'Running') { throw 'Agent is still running' }

& $Python -m pip install --no-index --no-deps --force-reinstall (Join-Path $PatchRoot 'wheelhouse\desktop_collection_agent-0.2.0-py3-none-any.whl')
if ($LASTEXITCODE -ne 0) { throw 'desktop-collection-agent install failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependency check failed' }
```

## 3. 验证并启动

```powershell
& $Python -c @'
import inspect
from desktop_agent.runtime.supervisor import AgentSupervisor
from desktop_agent.source_adapters.zhaopin import ZhaopinNoResults, ZhaopinSourceAdapter

assert issubclass(ZhaopinNoResults, RuntimeError)
assert 'raise ZhaopinNoResults' in inspect.getsource(ZhaopinSourceAdapter.collect)
assert 'failure_code="no_results"' in inspect.getsource(AgentSupervisor._run_lease)
print('no-results-fix.1 code verified')
'@
if ($LASTEXITCODE -ne 0) { throw 'Agent patch code verification failed' }
& 'C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe' --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Agent config check failed' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\JobCollectionAgent\release\agent\windows\start.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Agent start failed' }
Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'Job Collection Desktop Agent' | Format-List LastRunTime, LastTaskResult
Get-Content -LiteralPath 'C:\JobCollectionAgent\release\data-node-01\logs\desktop-agent.log' -Tail 80
```

计划任务应为 `Running`；运行中的 `LastTaskResult` 通常是 `267009` (`0x41301`)。
下一次无结果任务应记录 `task search returned no results`，服务端任务状态应为
`failed`、失败码为 `no_results`，租约字段为空，Profile 恢复为 `ready`。再确认 Agent
能够领取后续任务。具体任务 `721b14a3-3e41-423f-8d80-e45edcb0240e` 的实时状态
仍需在其所属环境核对；仅安装补丁不能证明该租约已经释放。

## 4. 回退

停止 Agent，用升级前保留的可信 `desktop_collection_agent-0.2.0-py3-none-any.whl`
执行同一条 `pip install --no-index --no-deps --force-reinstall` 命令；运行配置检查后再
启动。不要删除浏览器 Profile 或 spool。由于前后版本号均为 0.2.0，按 wheel 校验值
和实际行为确认回退结果。
