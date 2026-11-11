# Desktop Agent 0.4.0 升级说明

本版本将 crawler-api、crawler-contracts 和 desktop-collection-agent 统一升级到
`0.4.0`，主要变化是桌面 Agent 新增可选的 Playwright 浏览器引擎：

- 浏览器层拆分为与引擎无关的接口，Selenium 仍是默认引擎，现有部署行为不变。
- 新增 `BROWSER_ENGINE=playwright`，通过 `agent.env` 选择引擎；`BROWSER_NAME` 仍控制
  浏览器类型。
- Windows 的 Playwright 不下载额外 Chromium，按 `BROWSER_NAME` 复用系统 Chrome 或
  Edge（`chrome` 使用 `chrome` channel，`edge` 使用 `msedge`，`auto` 先 Chrome 再 Edge）。
- Linux 的 `install.sh` 在 `BROWSER_ENGINE=playwright` 时通过国内镜像自动安装捆绑
  Chromium，详见 RUNBOOK §5.1。
- crawler-api 镜像构建固定 Python 基础镜像摘要、使用 `uv.lock` 约束依赖并改用国内
  PyPI 镜像；生产 Compose 的 API 端口显式绑定 `0.0.0.0:8080`。

本次升级不新增数据库迁移。请先部署同一发布源码构建的
`job-collection-engine/crawler-api:0.4.0` 镜像，再升级 Windows Agent。API 与 Agent
均使用更新后的 crawler-contracts，不能只升级 Agent。

离线包为 `desktop-agent-windows-x86_64-0.4.0.zip`，面向 Windows x64 / CPython 3.12，
包含两个 0.4.0 wheel、目标平台依赖、安装启停脚本、INSTALL.md 和 RUNBOOK。wheelhouse
新增 Playwright 及其 `greenlet` 依赖，`--no-index` 安装必须使用完整离线包，不能沿用
旧版 wheelhouse。先从可信发布渠道取得 ZIP 与 `.zip.sha256`，校验外层 SHA-256 后解压，
再校验包内文件。以下命令由最终运行 Agent 的专用交互桌面账号执行，路径按实际安装位置
调整。

```powershell
$PatchRoot = 'C:\JobCollectionAgent\staging\0.4.0'
Get-Content (Join-Path $PatchRoot 'SHA256SUMS') | ForEach-Object { if ($_ -match '^([0-9a-f]{64})\s+(.+)$') { $Expected = $Matches[1]; $Relative = $Matches[2] -replace '^\./', ''; $Actual = (Get-FileHash -LiteralPath (Join-Path $PatchRoot $Relative) -Algorithm SHA256).Hash.ToLower(); if ($Actual -ne $Expected) { throw "Package member checksum mismatch: $Relative" } } }
$Python = 'C:\JobCollectionAgent\release\venv\Scripts\python.exe'
$Config = 'C:\JobCollectionAgent\release\agent-node-01.env'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\stop.ps1
if ($LASTEXITCODE -ne 0) { throw 'Agent stop failed' }
& $Python -m pip install --no-index --find-links (Join-Path $PatchRoot 'wheelhouse') crawler-contracts==0.4.0 desktop-collection-agent==0.4.0
if ($LASTEXITCODE -ne 0) { throw 'Agent package upgrade failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
& $Python -c "from importlib.metadata import version; assert version('crawler-contracts') == '0.4.0'; assert version('desktop-collection-agent') == '0.4.0'; print('Package versions verified')"
if ($LASTEXITCODE -ne 0) { throw 'Package version check failed' }
& 'C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe' --config $Config
if ($LASTEXITCODE -ne 0) { throw 'Agent configuration check failed' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\start.ps1
if ($LASTEXITCODE -ne 0) { throw 'Agent start failed' }
```

## 切换到 Playwright（可选）

默认保持 Selenium，不需要修改配置。确认要切换时：

1. 协作停止 Agent，确认没有残留浏览器进程且 Profile 锁已释放。
2. 备份 `agent.env`、SQLite spool 和 Browser Profile，不要导出 Cookie。
3. 在 `agent.env` 中设置 `BROWSER_ENGINE=playwright`，并确认 `BROWSER_NAME` 对应系统
   已安装的浏览器。
4. 重新执行 `install.ps1`，再运行配置预检。
5. 启动 Agent，确认 Session、heartbeat、Profile 为 `ready`，并完成一次受控任务。

不要在切换引擎的同时更换 `BROWSER_PROFILE_ROOT`；切换属于版本级变更，需要单独验证。
回退到 Selenium 时按同样步骤改回 `BROWSER_ENGINE=selenium` 或删除该行，并重新执行
`install.ps1`。

## 升级后验证

- 检查 API `/openapi.json` 的 `info.version` 为 `0.4.0`。
- 确认 Agent 日志出现新的 Session 和持续 heartbeat，Profile 状态为 `ready`。
- 执行一次受控采集任务，确认 `source_listings` 和 `parsed_listings` 正常写入。
- 使用 Playwright 时，日志不得出现
  `no Playwright Chromium or Edge browser is available`。

## 回退

回退时先停止 Agent，用保存的原版本 wheel 离线安装原版本，再执行配置预检并启动。
API 与 Agent 应回退到匹配的发布版本；不要删除 Profile、凭据或 spool。Linux 执行机
按 RUNBOOK 使用旧 wheel 重建 venv，并重新运行 `install.sh`。
