# Desktop Agent 0.3.0 升级说明

本版本将 crawler-api、crawler-contracts 和 desktop-collection-agent 统一升级到
`0.3.0`，包含多页采集与重复岗位处理修复：

- 分页正常结束的条件为达到用户指定页数，或来源检索已到末尾且不再返回岗位数据。
- 中间页返回重复岗位时继续采集，重复岗位不会触发提前完成。
- 重复记录交给 crawler-api 存储层幂等处理，避免多次采集计划重复录入同一岗位。

请先部署同一发布源码构建的 `job-collection-engine/crawler-api:0.3.0` 镜像，再升级
Windows Agent。API 与 Agent 均使用更新后的 crawler-contracts，不能只升级 Agent。
本次修复不新增数据库迁移。

离线包为 `desktop-agent-windows-x86_64-0.3.0.zip`，面向 Windows x64 / CPython 3.12，
包含两个 0.3.0 wheel、目标平台依赖、安装启停脚本、INSTALL.md 和 RUNBOOK。
先从可信发布渠道取得 ZIP 与 `.zip.sha256`，校验外层 SHA-256 后解压，再校验包内文件。
以下命令由最终运行 Agent 的专用交互桌面账号执行，路径按实际安装位置调整。

```powershell
$PatchRoot = 'C:\JobCollectionAgent\staging\0.3.0'
Get-Content (Join-Path $PatchRoot 'SHA256SUMS') | ForEach-Object { if ($_ -match '^([0-9a-f]{64})\s+(.+)$') { $Expected = $Matches[1]; $Relative = $Matches[2] -replace '^\./', ''; $Actual = (Get-FileHash -LiteralPath (Join-Path $PatchRoot $Relative) -Algorithm SHA256).Hash.ToLower(); if ($Actual -ne $Expected) { throw "Package member checksum mismatch: $Relative" } } }
$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Config = 'C:\JobCollectionAgent\release\agent-node-01.env'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\stop.ps1
if ($LASTEXITCODE -ne 0) { throw 'Agent stop failed' }
& $Python -m pip install --no-index --find-links (Join-Path $PatchRoot 'wheelhouse') crawler-contracts==0.3.0 desktop-collection-agent==0.3.0
if ($LASTEXITCODE -ne 0) { throw 'Agent package upgrade failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
& $Python -c "from importlib.metadata import version; assert version('crawler-contracts') == '0.3.0'; assert version('desktop-collection-agent') == '0.3.0'; print('Package versions verified')"
if ($LASTEXITCODE -ne 0) { throw 'Package version check failed' }
& 'C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe' --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Agent configuration check failed' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\start.ps1
if ($LASTEXITCODE -ne 0) { throw 'Agent start failed' }
```

升级前保留当前版本的可信 wheel、配置和日志；保留现有浏览器 Profile、凭据和 spool。
首次安装使用包内 INSTALL.md。升级后检查 API `/openapi.json` 的 `info.version` 为
`0.3.0`，观察 Agent 日志中的 `source_listings`、`parsed_listings` 和连续页码，验证指定
50 页的任务在仍有岗位数据时持续采集，直到指定页数或检索末尾。

回退时先停止 Agent，用保存的原版本 wheel 离线安装原版本，再执行配置预检并启动。
API 与 Agent 应回退到匹配的发布版本；不要删除 Profile、凭据或 spool。
