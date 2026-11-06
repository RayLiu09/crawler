# Desktop Agent 0.2.0 智联省级地区代码补丁

此补丁更新 `desktop-collection-agent` wheel 中的智联招聘地区表，加入云南、广东、湖北等省级地区代码，并支持完整省名和常用简称。例如任务地区为 `云南` 或 `云南省` 时，都会生成 `jl=554`。

补丁只替换 Agent wheel，不修改 crawler-api、`crawler-contracts`、Agent ID、设备凭据、环境文件、计划任务、浏览器 Profile 或本地 spool。wheel 版本仍为 `0.2.0`，因此安装时必须使用 `--force-reinstall`。

以下命令以 Node 01 为例。Node 02 将 `$Config` 改为 Node 02 的配置文件；停止和启动命令仍由实际运行计划任务的 Windows 交互用户执行。

## 1. 校验并解压补丁

```powershell
$PatchZip = 'C:\JobCollectionAgent\staging\desktop-agent-windows-x86_64-0.2.0-zhaopin-province-codes-fix.1.zip'
$PatchRoot = 'C:\JobCollectionAgent\staging\zhaopin-province-codes-fix.1'
$ExpectedHash = ((Get-Content -LiteralPath "$PatchZip.sha256" -Raw).Trim() -split '\s+')[0].ToLower()
$ActualHash = (Get-FileHash -LiteralPath $PatchZip -Algorithm SHA256).Hash.ToLower()
if ($ActualHash -ne $ExpectedHash) { throw "patch ZIP SHA-256 mismatch: $ActualHash" }

Expand-Archive -LiteralPath $PatchZip -DestinationPath $PatchRoot -Force

Get-Content -LiteralPath (Join-Path $PatchRoot 'SHA256SUMS') | ForEach-Object {
    if ($_ -match '^([0-9a-f]{64})\s+(.+)$') {
        $Relative = $Matches[2] -replace '^\./', ''
        $Path = Join-Path $PatchRoot $Relative
        $Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
        if ($Hash -ne $Matches[1]) { throw "patch member SHA-256 mismatch: $Relative" }
    } else {
        throw "invalid SHA256SUMS line: $_"
    }
}
Write-Output 'patch checksums verified'
```

外层 `.sha256` 文件和包内 `SHA256SUMS` 都必须通过可信发布记录核对后再继续。

## 2. 停止 Agent 并替换 wheel

```powershell
$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Config = 'C:\JobCollectionAgent\release\agent-node-01.env'
$Wheel = Join-Path $PatchRoot 'wheelhouse\desktop_collection_agent-0.2.0-py3-none-any.whl'

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\JobCollectionAgent\release\agent\windows\stop.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Agent did not stop cleanly' }
if ((Get-ScheduledTask -TaskName 'Job Collection Desktop Agent').State -eq 'Running') {
    throw 'Agent is still running'
}

& $Python -m pip install --no-index --no-deps --force-reinstall $Wheel
if ($LASTEXITCODE -ne 0) { throw 'desktop-collection-agent install failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependency check failed' }
```

## 3. 验证地区代码和配置

```powershell
& $Python (Join-Path $PatchRoot 'verify-province-codes.py') --config $Config
if ($LASTEXITCODE -ne 0) { throw 'province-code or effective configuration verification failed' }

& 'C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe' `
  --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Agent config check failed' }
```

验证脚本会核对全部 27 个省级地区及其简称，并验证实际配置所用的地区表，不启动浏览器，也不创建 Profile 目录。

如果配置了 `ZHAOPIN_CITY_CODES_FILE`，该外部文件会替换内置表，升级 wheel 不会修改它。请先备份外部表，再将包内 `province-codes.json` 的 27 条记录合并进去；同一代码保留一条完整名称记录（例如 `云南省`），不同时保留 `云南` 与 `云南省`，加载器会自动注册简称。合并后重新执行上面的验证命令，通过后再启动。已有自定义城市记录应保留。

## 4. 启动并检查日志

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\JobCollectionAgent\release\agent\windows\start.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Agent start failed' }
Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'Job Collection Desktop Agent' |
  Format-List LastRunTime, LastTaskResult
```

重新提交或重试任务时，确保请求中的 `keyword` 和 `region` 是 UTF-8 正确文本，例如 `酒店营销运营专员` 和 `云南省`。如果日志仍显示 `閰掑簵` 或 `浜戝崡鐪`，先按 UTF-8 读取原始日志并核对任务详情；地区表不应加入乱码名称。

## 回退

停止 Agent，使用升级前保存的可信 `desktop_collection_agent-0.2.0-py3-none-any.whl` 执行同一条 `pip install --no-index --no-deps --force-reinstall` 命令，运行配置预检后再启动。不要删除生产 Profile、spool 或凭据。
