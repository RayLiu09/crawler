# 桌面 Agent 安装与运行手册

## 1. 运行模型

桌面 Agent 必须运行在专用的交互式桌面账号中，因为真实采集需要显示 Chrome/Edge，
并允许人工完成登录或安全验证。禁止把 Agent 安装成 Windows Session 0 服务或 Linux
系统级 daemon：这两种方式都不能可靠访问用户桌面。

- Windows：使用当前专用用户的登录触发计划任务，`LogonType=Interactive`。
- Linux：使用当前专用用户的 systemd user service，由桌面 autostart 启动。
- 每个执行机使用唯一 `agentId`、deviceSecret、数据目录和 Profile 目录。
- 不启用 Linux user lingering；没有图形登录会话时不得启动可见浏览器任务。

## 2. 前置条件

1. Python 3.12 环境中安装已经锁定版本的 `desktop-collection-agent`。
2. 安装可见 Chrome 或 Edge，并使用专用桌面账号完成一次人工启动检查。
3. 控制中心通过 `provision-agent` 为本机预置唯一身份，一次性交付 deviceSecret。
4. 当前账号的系统凭据库可用：
   - Windows 使用 Credential Manager keyring backend；
   - Linux 使用已解锁的 Secret Service，并具有当前桌面会话的 DBus 环境。
5. 准备不含明文 secret 的绝对路径配置文件，可从
   `deploy/examples/desktop-agent.env.example` 开始修改。

## 3. 写入设备凭证

必须以最终运行 Agent 的同一个操作系统账号执行。命令采用隐藏输入并要求二次确认，
不会通过参数或标准输出传递 secret：

```text
desktop-collection-agent-credential set \
  --service job-collection-agent \
  --username <agentId>
```

检查凭证是否存在，不会读取或输出凭证内容：

```text
desktop-collection-agent-credential status \
  --service job-collection-agent \
  --username <agentId>
```

配置文件只保存定位信息：

```text
AGENT_DEVICE_SECRET_KEYRING_SERVICE=job-collection-agent
AGENT_DEVICE_SECRET_KEYRING_USERNAME=<agentId>
```

托管安装器会拒绝包含 `AGENT_DEVICE_SECRET=` 的配置文件。直接环境变量 secret 只保留给
受控开发和临时诊断，不用于执行机托管安装。

## 4. 配置预检

预检会验证配置结构并实际确认当前账号能够从 keyring 读取凭证，但不会输出凭证：

```text
desktop-collection-agent-check-config --config <absolute-agent.env>
```

智联城市代码默认从 Agent 包内的 `city_codes.json` 加载。后续需要调整时，将完整 JSON 表
复制到持久目录，在 `agent.env` 中设置 `ZHAOPIN_CITY_CODES_FILE=<绝对路径>`。外部文件
替换默认表，城市名称不带“市”后缀，代码写为数字字符串。编辑后重新运行配置预检，再重启
Agent；重启前正在运行的进程不会读取新映射。

## 5. Linux 安装

在已经登录图形桌面的专用用户终端中执行，不能使用 root：

```text
bash deploy/agent/linux/install.sh \
  --executable /opt/job-collection-agent/bin/desktop-collection-agent \
  --config /absolute/path/agent.env
```

安装内容：

- `~/.config/job-collection-agent/agent.env`，权限 `0600`；
- `~/.config/systemd/user/desktop-collection-agent.service`；
- `~/.config/autostart/job-collection-agent.desktop`；
- 当前桌面会话的 DISPLAY、Wayland、XAUTHORITY 和 DBus 环境导入 user manager。

systemd 使用 `KillMode=mixed`：正常停止只向 Agent 主进程发送 SIGTERM，给 Runtime
180 秒完成任务安全点、WebDriver 关闭和 Profile 解锁；超时后才清理整个 unit cgroup。

常用命令：

```text
systemctl --user status desktop-collection-agent.service
systemctl --user restart desktop-collection-agent.service
journalctl --user -u desktop-collection-agent.service
bash deploy/agent/linux/uninstall.sh
```

卸载默认保留 Agent 配置、SQLite spool 和 Browser Profile，防止误删尚未确认的解析记录
或登录状态；确认完成数据恢复和凭证吊销后再人工删除。

## 6. Windows 安装

在最终运行 Agent 的专用桌面账号 PowerShell 中执行，不要改为 SYSTEM 账号：

```text
powershell -ExecutionPolicy Bypass -File deploy\agent\windows\install.ps1 `
  -ExecutablePath C:\JobCollectionAgent\venv\Scripts\desktop-collection-agent.exe `
  -ConfigPath C:\staging\agent.env
```

安装器将：

- 把配置和启动器复制到当前用户的 `%LOCALAPPDATA%\JobCollectionAgent`；
- 将目录 ACL 收紧为当前用户和 SYSTEM；
- 注册 `Interactive` 登录触发计划任务；
- 设置一分钟间隔、最多十次的失败重启；
- 禁止同一 Agent 任务并行启动多个实例。

正常启停必须使用配套脚本：

```text
powershell -File deploy\agent\windows\stop.ps1
powershell -File deploy\agent\windows\start.ps1
powershell -File deploy\agent\windows\uninstall.ps1
```

`stop.ps1` 创建本地 stop marker，Agent 检测后停止新 claim，并在 Adapter 安全点取消当前
任务。脚本等待最多 180 秒，只有协作停止超时才强制停止计划任务。卸载同样保留配置、
spool 和 Profile，需在确认恢复和吊销后单独处置。

## 7. 设备凭证轮换

1. 在控制中心执行受控 `rotate-agent-credential`，旧 Session 会被吊销。
2. 使用本机停止脚本协作停止 Agent。
3. 以同一操作系统账号执行 credential `set`，隐藏输入新 deviceSecret。
4. 执行 config check，确认新凭证可读。
5. 启动 Agent，确认新 Session、heartbeat 和 Profile 状态。
6. 扫描安装日志和 shell history，确认没有明文 secret。

不得先删除旧 keyring 项再向控制中心申请轮换；如果新凭证交付失败，会同时失去新旧
凭证。应在受控窗口中完成“控制中心轮换 -> 本机覆盖 -> 立即验证”。

## 8. 升级与回滚

1. 协作停止 Agent，确认没有活动浏览器进程且 Profile 锁已释放。
2. 备份当前 wheel/venv、`agent.env` 和 SQLite spool；不复制或导出浏览器 Cookie。
3. 在新版本隔离环境运行 config check 和本地测试后替换 executable。
4. 启动并确认 Session、heartbeat、Profile `ready` 和一次受控任务。
5. 回滚时恢复旧 executable，不回退或删除 spool；控制中心协议必须仍兼容冻结的 0.4
   Agent API。

## 9. 故障处理

- `credential store is unavailable`：确认以专用用户登录、keyring backend 可用，Linux
  Secret Service 已解锁且 DBus 环境已导入。
- Profile 为 `unavailable`：检查锁文件登记的 PID/创建时间和浏览器残留，不得直接删除
  锁并继续运行。先停止 Agent 并确认登记进程退出，再按受控恢复流程重启。
- Windows stop 超时：脚本会强制停止任务并返回失败；下次启动前必须检查残留浏览器和
  Profile 锁。
- Linux systemd 超时：`KillMode=mixed` 会清理 unit cgroup；仍需检查 Profile 锁并保留
  故障证据。
