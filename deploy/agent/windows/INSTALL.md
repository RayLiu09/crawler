# Desktop Collection Agent - Windows x64

本安装包面向 Windows x64 和 CPython 3.12。所有凭据写入、配置预检和计划任务安装命令，
都必须由最终运行 Agent 的专用交互桌面账号执行。Agent 不能安装为 Windows Service、
SYSTEM 账号或 Session 0 进程。

## 前置条件

- 64 位 CPython 3.12；
- 可见的 Chrome 或 Edge，以及匹配的 WebDriver/Selenium Manager 缓存；
- 能访问 crawler-api 和目标来源站点；
- 控制中心已经预置 `agentId`，并通过安全渠道一次性交付 `deviceSecret`。

## 离线安装 Python 包

在解压后的安装包根目录执行：

```powershell
py -3.12 -m venv C:\JobCollectionAgent\release\venv
C:\JobCollectionAgent\release\venv\Scripts\python.exe -m pip install --no-index --find-links .\wheelhouse crawler-contracts==0.2.0 desktop-collection-agent==0.2.0
```

安装前用可信发布渠道提供的 SHA-256 校验 ZIP；解压后按 `SHA256SUMS` 校验包内文件。

## 写入凭据、预检和安装

配置文件不得包含 `AGENT_DEVICE_SECRET=`。使用以下单行命令，避免复制 PowerShell 的
`PS C:\>` 或 `>>` 提示符：

```powershell
C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-credential.exe set --service job-collection-agent --username REPLACE_WITH_AGENT_UUID
C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe --config C:\JobCollectionAgent\release\agent-node-01.env
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\agent\windows\install.ps1 -ExecutablePath C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent.exe -ConfigPath C:\JobCollectionAgent\release\agent-node-01.env
```

若 `Register-ScheduledTask` 返回 `0x80070005`，请保持登录账号不变，以“管理员身份运行”
PowerShell 后重新执行安装命令。不要切换为其他管理员账号，否则计划任务会绑定到错误的
交互用户。最终任务仍使用当前 Agent 用户、`Interactive` 和 `RunLevel Limited` 运行。

安装完成后阅读 `agent\RUNBOOK.md`，并检查：

```powershell
Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' | Format-List TaskName,State
Get-ScheduledTaskInfo -TaskName 'Job Collection Desktop Agent' | Format-List LastRunTime,LastTaskResult
```
