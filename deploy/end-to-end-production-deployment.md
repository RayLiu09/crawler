# 岗位数据采集引擎端到端生产级安装部署手册

## 1. 文档目的

本文用于准备和执行岗位数据采集引擎 0.4 的真实端到端验证环境。它覆盖控制中心、
PostgreSQL、验收调用方以及至少两台桌面 Agent 执行机的安装、配置、上线、验证、证据
留存和回滚。

本轮尚未接入上游应用，因此采用独立的 `e2e-acceptance` 验收 tenant：验收人员使用
tenantKey 申请短期 Caller Token，再直接调用正式的 Collection Job API。禁止绕过 API
向数据库插入 Job，也不使用 Fake Adapter、测试 Actor、离线 DOM 或单元测试代替真实
浏览器验收。

本轮 Job 请求不携带 `callback`，以下能力明确不在本轮通过范围内：

- 上游应用注册、上游业务流程以及从上游下发 Job；
- Callback Token 的签发和轮换；
- Webhook Outbox 的真实 HTTP 投递、重试和回放。

Webhook Dispatcher 可以保持生产默认启用；没有 callback 配置时不会为 Job 产生投递。
这不等于 Webhook 已通过验收。后续接入上游时必须单独完成回调链路验收。

开始安装前先阅读 [前置制品与环境检查报告](../dist/PREPARATION-REPORT.md)。当前已准备的
制品为：

- `../dist/desktop-agent-windows-x86_64-0.1.0.zip`：Windows x64 / CPython 3.12 Agent
  离线安装包，包含 wheelhouse、Windows 安装脚本和 RUNBOOK；
- `../dist/crawler-api-0.1.0.tar`：可通过 `docker load` 导入的 crawler-api 镜像包；
- `../dist/crawler-api-0.1.0.tar.sha256`：镜像包完整性校验文件。

制品导入前必须按报告中的 SHA-256 校验；Windows Agent 仍需在真实 Windows x64 主机上
完成一次干净安装和配置预检，Linux 构建机的 wheel 标签检查不能替代该步骤。

相关基线资料：

- [生产 Compose](docker/docker-compose.prod.yml)
- [数据库初始化说明](database/README.md)
- [桌面 Agent 安装与运行手册](agent/RUNBOOK.md)
- [双执行机验收场景](../docs/acceptance-8.4.md)
- [生产 API 环境变量示例](examples/crawler-api.production.env.example)

## 快速执行路径

本手册把“编译打包”和“安装部署”分开。构建机只负责生成并校验不可变制品；控制中心
服务器只导入已校验的 API 镜像；Windows 执行机只安装已校验的 Agent ZIP。不要在控制
中心服务器临时修改源码，也不要在 Agent 执行机联网解析 Python 依赖。

### 交付物

构建完成后必须交付以下文件，并通过受控制品渠道传输：

| 文件 | 用途 |
|---|---|
| `dist/crawler-api-0.1.0.tar` | crawler-api、migration 共用的 Docker 镜像包 |
| `dist/crawler-api-0.1.0.tar.sha256` | API 镜像包校验值 |
| `dist/desktop-agent-windows-x86_64-0.1.0.zip` | Windows x64 / CPython 3.12 离线 Agent 安装包 |
| `dist/desktop-agent-windows-x86_64-0.1.0.zip.sha256` | Agent 安装包校验值 |

当前已生成的 Agent ZIP SHA-256 为：

```text
2916f9711cbd4498965430a760e4532e2643dbc97fd6bd6388eff439ea312729
```

### 部署顺序

1. 在构建机固定 release commit，运行质量门禁，构建 API 镜像和 Windows Agent ZIP。
2. 在控制中心服务器导入 API tar，准备密钥、tenant allowlist 和数据库连接参数。
3. 根据环境选择生产 Compose（同机 PostgreSQL 容器）或开发 Compose（已有外部 PostgreSQL）。
4. 运行 migration，检查 API readiness、Alembic head、时区和 tenant 同步。
5. 通过控制中心命令预置至少两个独立 Agent 身份，并分别安全交付 deviceSecret。
6. 在 Windows 交互桌面账号中离线安装 wheel，写入 keyring，执行配置预检和计划任务安装。
7. 先验证 Node 01 的 Session、heartbeat、Profile，再启动 Node 02。
8. 验收终端通过 tenantKey 申请 Caller Token，直接调用正式 Job API 完成真实采集。

每一步都有“停止条件”；出现密钥、allowlist、数据库或身份错误时停止，不通过重复重启
掩盖故障。

## 2. 系统边界与部署拓扑

```text
                    受控管理网 / 验收网络

  验收操作终端
  - 保存验收 tenantKey
  - 申请短期 Caller Token
  - 直接调用 Job API
           |
           | HTTP/HTTPS 8080（仅允许受信网段）
           v
  +------------------------------------------------------+
  | 控制中心服务器                                       |
  |                                                      |
  |  Docker Compose                                      |
  |  +----------------+       crawler-internal           |
  |  | crawler-api    |<----------------------------+    |
  |  | FastAPI :8080  |                             |    |
  |  +----------------+       +------------------+   |    |
  |          ^                | PostgreSQL 18.1  |---+    |
  |          |                | 不发布 5432      |        |
  |          |                | 持久化 Docker 卷 |        |
  |          |                +------------------+        |
  |          |      migrate：一次性 alembic upgrade head |
  +----------|-------------------------------------------+
             |
             | Agent 仅出站访问控制中心
       +-----+--------------------------+
       |                                |
       v                                v
  执行机 Node 01                   执行机 Node 02
  - 专用交互桌面账号                - 专用交互桌面账号
  - 可见 Chrome/Edge                - 可见 Chrome/Edge
  - 独立 agentId/deviceSecret       - 独立 agentId/deviceSecret
  - 独立 SQLite spool/Profile       - 独立 SQLite spool/Profile
       |                                |
       +---- HTTPS 访问真实来源站点 -----+
```

身份边界必须保持如下约束：

- 验收调用方和 Agent 是两套身份域，凭证不可互换；
- Caller 使用 tenantKey 换取短期 Caller Token，并以 scope 访问 Job API；
- Agent 使用预置的 `agentId + deviceSecret` 换取短期 Agent Session；
- Agent 是全局执行节点，不绑定 tenant，不接收 tenantId、clientId 或 callback 信息；
- Agent 不访问 PostgreSQL；PostgreSQL 只在 Compose 私网中供控制中心访问；
- 控制面只保存业务无关的运行数据和解析后的原始岗位记录，不保存 HTML、截图、Cookie、
  Profile 内容或上游派生业务结果。

## 3. 环境清单与准入门槛

### 3.1 控制中心服务器

生产验证前至少确认：

| 项目 | 要求 |
|---|---|
| 操作系统 | 受支持的 64 位 Linux，已完成安全更新 |
| 容器运行时 | Docker Engine 与 Docker Compose v2，服务设置为开机启动 |
| 源码/制品 | 固定到已评审的 release commit，不从浮动分支现场部署 |
| PostgreSQL | `postgres:18.1-alpine`，独立容器，数据卷持久化，不发布 5432 |
| 时区 | 主机、容器、数据库会话均为 `Asia/Shanghai` |
| 时间同步 | NTP/chrony 正常，控制中心与执行机时差不超过 5 秒 |
| API 端口 | 8080 只向执行机和受控验收网段开放，不暴露公网 |
| 磁盘 | Docker 数据目录有容量告警；数据库卷与备份目录不能共用单一故障介质 |
| 备份 | 部署前全量备份，验证期间定时备份，并已完成隔离环境恢复演练 |

CPU、内存和磁盘容量必须根据并发 Job、记录保留期和数据库增长量评估。初始数据库连接池
为 10，`DATABASE_MAX_OVERFLOW=0`；不能只按“容器能够启动”判断生产容量充足。

### 3.2 桌面执行机

Node 01 和 Node 02 必须是两个独立执行环境；不能在同一主机复制两个进程冒充双节点。
每台执行机要求：

- Python 3.12；
- 可见的 Chrome，或 Windows 上可见的 Edge；
- 匹配浏览器版本的 WebDriver，或已由同一运行账号预热完成的 Selenium Manager 缓存；
- 可人工操作的图形登录会话；
- 唯一 `agentId`、deviceSecret、本地数据目录和浏览器 Profile 根目录；
- 操作系统凭据库可用；Linux 需要已解锁的 Secret Service 和当前桌面 DBus；
- 能出站访问控制中心 API、DNS、NTP 和 `zhaopin.com` 所需 HTTPS 域名；
- 不开放 Agent 入站服务，不允许 Agent 直连 PostgreSQL。

当前 Zhaopin Adapter 的已评审城市映射只包含 `北京` 和 `北京市`。真实验收请求应使用这
两个值之一；增加其他城市必须先补充经过验证的城市代码和自动化覆盖。

### 3.3 验收操作终端

操作终端需要 `curl`、`jq`、`openssl`、`rg`，并且只能由验收人员访问。Caller Token 和
tenantKey 不得出现在共享 shell history、CI 日志或验收报告中。建议在 Linux 的内存文件
系统（如 `/dev/shm`）保存短期请求文件，并在会话结束时删除。

## 4. 网络、DNS 和防火墙

部署前由网络管理员确认以下最小流量：

| 源 | 目标 | 端口 | 方向 | 用途 |
|---|---|---:|---|---|
| 验收操作终端 | crawler-api | 8080/TCP 或反向代理端口 | 出站 | Caller Token、Job API、健康检查 |
| Node 01/02 | crawler-api | 8080/TCP 或反向代理端口 | 出站 | Session、heartbeat、claim、记录上传 |
| Node 01/02 | 来源站点 | 443/TCP | 出站 | 真实页面采集与人工登录/验证 |
| 全部主机 | 内部 DNS、NTP | 环境指定 | 出站 | 名称解析与时间同步 |
| crawler-api | PostgreSQL | 5432/TCP | Compose 私网 | 数据访问；主机和外网不发布 |

若 API 跨越不受信任网络，必须在受管反向代理或负载均衡器终止 TLS，并把 Agent 的
`CRAWLER_API_URL` 配置为 HTTPS。当前 Compose 直接发布 HTTP 8080，只有在隔离且受信任
的内网中才可直接使用。无论 HTTP 还是 HTTPS，都必须限制来源网段。

`GET /internal/metrics` 当前与 API 使用同一监听端口且没有独立认证，只能由受控监控网络
访问。若通过反向代理发布 API，应在代理层拒绝普通调用方访问 `/internal/*`；不能把该
路径随公网 API 一并开放。

部署前分别从两台执行机验证 DNS、API 端口和来源站点连接；不要用控制中心自身的连接
结果代替执行机检查。

## 5. 目录、账号和 Secret 准备

以下路径为手册约定，可按企业规范替换，但必须使用绝对路径：

```text
/srv/job-collection-engine/                 release 工作目录
/etc/job-collection-engine/                 生产配置目录，root:root 0700
/etc/job-collection-engine/production.env   Compose 插值配置，0600
/etc/job-collection-engine/crawler-api.keys.env  控制面密钥，0600
/etc/job-collection-engine/tenant-allowlist.yml  tenant 白名单，root:root 0644
/var/backups/job-collection-engine/          数据库备份，受控存储
/var/log/job-collection-evidence/            脱敏验收证据
```

先创建受控目录，并把 release 内容部署到 `/srv/job-collection-engine`。生产配置、密钥、
Agent provisioning 输出和验收调用方凭证均不得放进 Git 工作树。

### 5.1 生成控制面密钥

项目脚本会一次性生成五个互不复用的随机值，不打印具体内容，并拒绝覆盖已有文件：

```bash
cd /srv/job-collection-engine
umask 077
python scripts/generate_initial_secrets.py \
  --output /etc/job-collection-engine/crawler-api.keys.env
chmod 0600 /etc/job-collection-engine/crawler-api.keys.env
```

文件包括：

- `API_TOKEN_HASH_KEY`
- `AGENT_DEVICE_CREDENTIAL_HASH_KEY`
- `AGENT_TOKEN_HASH_KEY`
- `CALLBACK_TOKEN_ENCRYPTION_KEY`（32 字节、无填充 base64url，长度 43）
- `CALLBACK_TOKEN_FINGERPRINT_KEY`

这些密钥是持久化数据可验证性或可解密性的根。容器重建时必须注入原值，不能每次启动
重新生成。轮换必须按独立变更执行；尤其不能在仍有有效 callback ciphertext 时直接丢弃
旧的加密密钥。当前 Compose 使用环境变量传入密钥，因此具备 Docker 管理权限的人员可
读取容器环境；生产主机的 Docker 权限必须视同 root 权限管理。

### 5.2 准备生产 Compose 环境

复制示例后由 Secret 管理系统或受控运维流程写入真实值：

```bash
install -m 0600 \
  /srv/job-collection-engine/deploy/examples/crawler-api.production.env.example \
  /etc/job-collection-engine/production.env
```

`production.env` 至少补充或确认以下 Compose 参数：

```dotenv
CRAWLER_API_IMAGE=job-collection-engine/crawler-api:0.1.0
POSTGRES_IMAGE_TAG=18.1-alpine
POSTGRES_DATABASE=crawler
POSTGRES_USERNAME=crawler
POSTGRES_PASSWORD=replace-through-secret-delivery
TENANT_ALLOWLIST_FILE=/etc/job-collection-engine/tenant-allowlist.yml
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=0
CALLBACK_TOKEN_ENCRYPTION_KEY_VERSION=v1
WEBHOOK_METRICS_ENABLED=true
```

不得使用示例密码，不得把本项目历史开发数据库地址或密码复制到生产配置。Compose 中
PostgreSQL 与 API 同机但独立容器运行，应用链路固定 `POSTGRES_SSLMODE=disable`，因为
连接不离开私有 bridge；这不代表远程数据库连接可以关闭 TLS。

`API_ACCESS_TOKEN_TTL_SECONDS=3600`、`AGENT_SESSION_TTL_SECONDS=900`、
`LEASE_SECONDS=120`、`LEASE_RENEWAL_GRACE_SECONDS=30`、
`AGENT_OFFLINE_SECONDS=180` 以及 Webhook Dispatcher 参数当前使用应用默认值。现有生产
Compose 没有逐项透传这些可调参数；若上线评审决定修改默认值，必须先更新 Compose 的
service environment 并重新完成配置和故障窗口验证，不能只改示例 env 后假定已经生效。

### 5.3 建立验收 tenant 白名单

生成 256-bit 验收 tenantKey 并保存到企业 Secret 系统。下面示例使用受控本地文件，文件
内容绝不能提交、打印或写入验收报告：

```bash
umask 077
openssl rand -hex 32 > /etc/job-collection-engine/e2e-tenant.key
chmod 0600 /etc/job-collection-engine/e2e-tenant.key
```

计算可公开配置的 SHA-256 fingerprint。必须去掉生成文件末尾换行后再计算：

```bash
E2E_TENANT_HASH="$(tr -d '\r\n' \
  < /etc/job-collection-engine/e2e-tenant.key | sha256sum | cut -d ' ' -f 1)"
printf 'sha256:%s\n' "$E2E_TENANT_HASH"
unset E2E_TENANT_HASH
```

将输出 fingerprint 写入 `/etc/job-collection-engine/tenant-allowlist.yml`：

```yaml
version: 1
tenants:
  - tenantId: e2e-acceptance
    displayName: E2E Acceptance Operator
    enabled: true
    tenantKeyHashes:
      - sha256:REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS
    # scopes 省略时继承全部当前权限：
    # collection:write, collection:read, collection:control
    # 本轮不配置 callback，也不验证 Webhook。
    callbackAllowlist: []
```

白名单只能保存 hash，不保存原始 tenantKey。`callbackAllowlist: []` 是合法配置。bind
mount 后 crawler-api 以非 root 用户读取该文件，因此设置为 `root:root 0644`；原始
tenantKey、数据库密码和控制面 keys 仍保持 `0600`，二者不能混同。

## 6. 构建和打包发布制品

本节在独立构建机执行。构建机需要 Git、Python 3.12、`uv`、Docker、`pip`、`zip`、
`sha256sum`，并能访问 Python 包镜像和基础镜像仓库。构建机不保存生产 tenantKey、
Agent deviceSecret 或 PostgreSQL 密码。

### 6.1 固定源码和质量门禁

```bash
cd /workspace/refactor
git fetch --tags origin
git checkout --detach REPLACE_WITH_RELEASE_COMMIT
test -z "$(git status --porcelain)"
git rev-parse HEAD | tee dist/RELEASE_COMMIT
uv sync --frozen
./scripts/verify.sh
```

`git status --porcelain` 非空、`uv sync --frozen` 或质量门禁失败时不得继续打包。

### 6.2 构建 crawler-api 镜像并导出 tar

API 和 migration 使用同一个镜像，镜像标签必须包含版本号，不能使用 `latest`：

```bash
cd /workspace/refactor
export RELEASE_VERSION=0.1.0
export CRAWLER_API_IMAGE="job-collection-engine/crawler-api:${RELEASE_VERSION}"

docker build --pull \
  --file services/crawler_api/Dockerfile \
  --tag "$CRAWLER_API_IMAGE" \
  .
docker image inspect "$CRAWLER_API_IMAGE" \
  --format 'image={{.RepoTags}} id={{.Id}} created={{.Created}}'

docker save --output "dist/crawler-api-${RELEASE_VERSION}.tar" "$CRAWLER_API_IMAGE"
sha256sum "dist/crawler-api-${RELEASE_VERSION}.tar" \
  | tee "dist/crawler-api-${RELEASE_VERSION}.tar.sha256"
```

把 tar 和 sha256 文件一起交付。服务器导入后必须使用同一个 `CRAWLER_API_IMAGE` 标签，
否则 Compose 可能找不到镜像或错误地从源码重建。

### 6.3 构建 Windows Agent 离线安装包

Agent 包必须在可解析 Windows x64 / CPython 3.12 wheel 的构建环境中生成。Linux 构建机
可以使用 pip 的目标平台参数下载纯 Python 和 `win_amd64` 二进制 wheel，但最终仍必须在
真实 Windows x64 主机做干净安装预检。

```bash
cd /workspace/refactor
export RELEASE_VERSION=0.1.0
export AGENT_STAGE="dist/desktop-agent-windows-x86_64-${RELEASE_VERSION}"
rm -rf "$AGENT_STAGE" "dist/desktop-agent-windows-x86_64-${RELEASE_VERSION}.zip"
mkdir -p "$AGENT_STAGE/wheelhouse" "$AGENT_STAGE/agent/windows" "$AGENT_STAGE/examples"

uv build --package crawler-contracts --out-dir dist
uv build --package desktop-collection-agent --out-dir dist

cp dist/crawler_contracts-${RELEASE_VERSION}-py3-none-any.whl \
   dist/desktop_collection_agent-${RELEASE_VERSION}-py3-none-any.whl \
   "$AGENT_STAGE/wheelhouse/"

printf '%s\n' \
  "dist/crawler_contracts-${RELEASE_VERSION}-py3-none-any.whl" \
  "dist/desktop_collection_agent-${RELEASE_VERSION}-py3-none-any.whl" \
  > dist/windows-agent-requirements.in
uv export --package desktop-collection-agent --frozen --no-dev \
  --no-emit-project --no-emit-workspace --no-hashes \
  --output-file dist/windows-agent-constraints.txt
uv pip compile dist/windows-agent-requirements.in \
  --constraints dist/windows-agent-constraints.txt \
  --python-platform x86_64-pc-windows-msvc \
  --python-version 3.12 \
  --only-binary :all: \
  --output-file dist/windows-agent-requirements.txt
python3.12 -m pip download \
  --requirement dist/windows-agent-requirements.txt \
  --only-binary=:all: \
  --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 \
  --dest "$AGENT_STAGE/wheelhouse"

cp deploy/agent/RUNBOOK.md "$AGENT_STAGE/agent/"
cp deploy/agent/windows/*.ps1 deploy/agent/windows/*.in "$AGENT_STAGE/agent/windows/"
cp deploy/agent/windows/INSTALL.md "$AGENT_STAGE/INSTALL.md"
cp deploy/examples/desktop-agent*.env.example "$AGENT_STAGE/examples/"
```

写入构建信息并生成包内校验清单。`SHA256SUMS` 不包含自身：

```bash
printf '%s\n' \
  "artifact=desktop-agent-windows-x86_64-${RELEASE_VERSION}" \
  "agentVersion=${RELEASE_VERSION}" \
  "contractsVersion=${RELEASE_VERSION}" \
  'targetOperatingSystem=windows' \
  'targetArchitecture=x86_64' \
  'pythonImplementation=CPython' \
  'pythonVersion=3.12' \
  'dependencyLockSource=uv.lock' \
  'installScriptCompatibility=Windows PowerShell 5.1 and PowerShell 7' \
  > "$AGENT_STAGE/BUILD-INFO.txt"
(
  cd "$AGENT_STAGE"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum
) > "$AGENT_STAGE/SHA256SUMS"
(
  cd "$AGENT_STAGE"
  sha256sum --check SHA256SUMS
  python3 -m zipfile -c "../desktop-agent-windows-x86_64-${RELEASE_VERSION}.zip" .
)
sha256sum "dist/desktop-agent-windows-x86_64-${RELEASE_VERSION}.zip" \
  | tee "dist/desktop-agent-windows-x86_64-${RELEASE_VERSION}.zip.sha256"
```

`windows-agent-constraints.txt` 来自冻结的 `uv.lock`，目标平台解析仍显式指定 Windows
x64/CPython 3.12。如果构建机上的 `pip download` 无法提供某个 `win_amd64` wheel，应停止打包并更换已审查
的 Windows 构建环境或企业制品源；不能把 Linux wheel 混入 Windows 包，也不能让执行机
在首次启动时从公网临时下载依赖。交付前在 Windows 执行机执行：

```powershell
(Get-FileHash -LiteralPath .\desktop-agent-windows-x86_64-0.1.0.zip -Algorithm SHA256).Hash.ToLower()
```

并与随包 `.sha256` 文件比较。Windows 安装脚本必须由最终交互用户实际运行一次，不能用
Linux 构建机的静态检查替代。

## 7. 控制中心首次部署

控制中心服务器只导入构建机交付的镜像，不在服务器现场编译。服务器仍需部署本目录下的
Compose 文件，因为它定义 migration、API、挂载和启动顺序。根据数据库形态二选一：

- 当前开发/E2E 环境使用已有外部 PostgreSQL：选择 `docker-compose.dev.yml`，不部署数据库；
- 正式生产环境由本项目部署 PostgreSQL 18：选择 `docker-compose.prod.yml`。

### 7.1 导入并核验镜像

以下命令均在控制中心服务器执行：

```bash
cd /srv/job-collection-engine
sha256sum --check dist/crawler-api-0.1.0.tar.sha256
docker load --input dist/crawler-api-0.1.0.tar
docker image inspect job-collection-engine/crawler-api:0.1.0 \
  --format 'id={{.Id}} created={{.Created}} tags={{json .RepoTags}}'
```

校验失败或镜像标签不是 `job-collection-engine/crawler-api:0.1.0` 时停止。不要临时改为
`latest`，也不要用服务器本地工作树重建一个同名镜像覆盖已验收制品。

### 7.2 选择数据库模式并准备环境文件

已有外部开发库使用：

```bash
CONTROL_COMPOSE=/srv/job-collection-engine/deploy/docker/docker-compose.dev.yml
```

生产环境同机独立 PostgreSQL 容器使用：

```bash
CONTROL_COMPOSE=/srv/job-collection-engine/deploy/docker/docker-compose.prod.yml
```

两种模式共用：

```bash
CONTROL_ENV=/etc/job-collection-engine/production.env
CONTROL_KEYS=/etc/job-collection-engine/crawler-api.keys.env
CONTROL_ALLOWLIST=/etc/job-collection-engine/tenant-allowlist.yml
control() {
  docker compose --env-file "$CONTROL_ENV" --env-file "$CONTROL_KEYS" \
    -f "$CONTROL_COMPOSE" "$@"
}
```

后续 `control ...` 是当前 shell 的简写；重新登录控制中心服务器后需要重新设置变量和函数。

`production.env` 必须包含固定镜像名：

```dotenv
CRAWLER_API_IMAGE=job-collection-engine/crawler-api:0.1.0
TENANT_ALLOWLIST_FILE=/etc/job-collection-engine/tenant-allowlist.yml
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=0
```

外部开发库还应配置真实的 `POSTGRES_HOST`、`POSTGRES_PORT`、`POSTGRES_DATABASE`、
`POSTGRES_USERNAME`、`POSTGRES_PASSWORD` 和 `POSTGRES_SSLMODE=prefer`。生产 Compose 则配置
`POSTGRES_IMAGE_TAG=18.1-alpine`、生产数据库账号和密码；容器私网连接使用
`POSTGRES_SSLMODE=disable`。数据库密码只写入权限为 `0600` 的环境文件，不放进 shell
命令行、镜像或 Compose 文件。

当前 E2E 外部开发库的非敏感参数如下，密码通过受控 Secret 交付填写：

```dotenv
POSTGRES_DRIVER=postgresql
POSTGRES_HOST=10.100.11.182
POSTGRES_PORT=5432
POSTGRES_DATABASE=crawler
POSTGRES_USERNAME=postgres_normal
POSTGRES_PASSWORD=REPLACE_FROM_SECRET_CHANNEL
POSTGRES_SSLMODE=prefer
APP_TIMEZONE=Asia/Shanghai
```

tenant allowlist 只包含 tenantKey 的 SHA-256 fingerprint 和回调地址白名单，不包含原始
tenantKey，因此可设为容器内运行用户可读：

```bash
sudo chown root:root "$CONTROL_ALLOWLIST"
sudo chmod 0644 "$CONTROL_ALLOWLIST"
sudo chmod 0600 "$CONTROL_ENV" "$CONTROL_KEYS"
```

如果 allowlist 为 `0600 root:root`，以非 root 用户运行的 crawler-api 会得到
`PermissionError: [Errno 13]`。不要通过把 API 容器改成 root 来规避该问题。

先独立验证挂载和 YAML schema；`python -c` 内容保持单行，避免缩进错误：

```bash
docker run --rm --mount "type=bind,src=${CONTROL_ALLOWLIST},dst=/run/secrets/tenant-allowlist.yml,readonly" job-collection-engine/crawler-api:0.1.0 python -c 'from pathlib import Path; from crawler_api.infrastructure.security.tenant_allowlist import load_tenant_allowlist; p=Path("/run/secrets/tenant-allowlist.yml"); a=load_tenant_allowlist(p); print([t.tenant_id for t in a.tenants])'
```

`src=` 为空说明当前 shell 没有定义 `CONTROL_ALLOWLIST`；先重新设置绝对路径，不要继续
执行。验证通过后检查 Compose 配置；`config --quiet` 不打印解析后的 Secret：

```bash
control config --quiet
```

### 7.3 执行 migration 并启动 API

已有外部 PostgreSQL：

```bash
control up --no-build migrate
control up -d --no-build crawler-api
```

生产 PostgreSQL 容器：

```bash
control up -d --no-build postgres
control up --no-build migrate
control up -d --no-build crawler-api
```

`--no-build` 是发布约束：它确保服务器使用刚才导入且经过校验的镜像。migration 必须
成功退出后再启动 API。检查时不得只看 API 容器是否存在：

```bash
control ps -a
control logs --no-log-prefix migrate
control exec -T crawler-api alembic current
```

准入结果应为：

- 生产模式 PostgreSQL 为 healthy；外部数据库模式能由 migration 和 readiness 正常连接；
- `migrate` 退出码为 0；
- Alembic current 为 `0009 (head)`；
- crawler-api 持续运行且没有 allowlist、密钥或数据库连接错误；
- 生产模式宿主机未发布 PostgreSQL 5432。

验证健康接口：

```bash
curl --fail-with-body --silent --show-error http://127.0.0.1:8080/health/live
curl --fail-with-body --silent --show-error http://127.0.0.1:8080/health/ready
```

`live` 和 `ready` 都应返回 `{"status":"ok"}`。`ready` 会实际检查数据库；503 不能通过
重启循环掩盖，应先查 migration、连接参数和数据库日志。

生产 Compose 可以直接确认时区、数据库版本和 schema：

```bash
control exec -T postgres \
  psql -U crawler -d crawler -X -v ON_ERROR_STOP=1 -c \
  "SELECT version(), current_setting('TimeZone');"
control exec -T postgres \
  psql -U crawler -d crawler -X -v ON_ERROR_STOP=1 -c \
  "SELECT version_num FROM alembic_version;"
```

外部数据库模式在受控数据库终端使用同样的两个 SQL 查询；不要为了执行 `psql` 临时给
API 镜像安装客户端，也不要把数据库密码放在命令历史中。

### 7.4 tenant 同步检查

API 每次启动会把 allowlist 同步到 `crawler.api_clients`。只查询非敏感字段：

```sql
SELECT tenant_id, display_name, allowed_scopes, status
  FROM crawler.api_clients
 WHERE tenant_id = 'e2e-acceptance';
```

应只有一条 `active` 记录，`allowed_scopes` 包含 write/read/control。修改 allowlist 后必须
重启 API 触发重新加载和同步；从 allowlist 删除或禁用 tenant 会禁用 client，并吊销其
未过期 Caller Token。

## 8. 数据库备份与恢复准入

首次创建真实 Job 前做一次逻辑备份，并把 release commit、Alembic revision、PostgreSQL
版本与备份文件关联记录。生产 PostgreSQL 容器执行：

```bash
install -d -m 0700 /var/backups/job-collection-engine
BACKUP_FILE="/var/backups/job-collection-engine/crawler-$(date +%Y%m%dT%H%M%S%z).dump"
control exec -T postgres \
  pg_dump -U crawler -d crawler --format=custom --no-owner --no-acl \
  > "$BACKUP_FILE"
chmod 0600 "$BACKUP_FILE"
control exec -T postgres pg_restore --list < "$BACKUP_FILE" > /dev/null
sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"
```

使用已有外部 PostgreSQL 时，由数据库运维在受控终端执行等价备份。示例通过权限为
`0600` 的 `PGPASSFILE` 取密码，不能把密码拼到 URI 或命令行：

```bash
install -d -m 0700 /var/backups/job-collection-engine
BACKUP_FILE="/var/backups/job-collection-engine/crawler-$(date +%Y%m%dT%H%M%S%z).dump"
PGPASSFILE=/secure/path/crawler.pgpass pg_dump --host=10.100.11.182 --port=5432 --username=postgres_normal --dbname=crawler --format=custom --no-owner --no-acl --file="$BACKUP_FILE"
PGPASSFILE=/secure/path/crawler.pgpass pg_restore --list "$BACKUP_FILE" > /dev/null
chmod 0600 "$BACKUP_FILE"
sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"
```

“备份命令成功”不等于可恢复。正式验证前必须在隔离 PostgreSQL 18 环境中恢复该备份，
运行 `alembic current`、核心表行数检查和 API readiness。禁止为了演练清空当前生产卷。
恢复步骤必须由数据库运维流程在独立实例执行，并保留恢复耗时与校验结果。

PostgreSQL 数据卷不得随 `docker compose down` 删除；本手册任何正常回滚都不使用
`down -v`。

## 9. 预置两台 Agent 身份

Agent 身份由控制中心运维命令预置，不通过自注册，也不需要 Agent Session。分别执行：

```bash
umask 077
control exec -T crawler-api provision-agent --display-name "E2E Desktop Node 01" \
  > /etc/job-collection-engine/node-01-provisioning.json
control exec -T crawler-api provision-agent --display-name "E2E Desktop Node 02" \
  > /etc/job-collection-engine/node-02-provisioning.json
chmod 0600 /etc/job-collection-engine/node-*-provisioning.json
```

每个 JSON 只输出一次 `agentId` 和原始 `deviceSecret`。立即通过企业 Secret 交付渠道分别
发给对应执行机，不要 `tee` 到日志，不要粘贴到工单。可读取非敏感 agentId：

```bash
jq -r .agentId /etc/job-collection-engine/node-01-provisioning.json
jq -r .agentId /etc/job-collection-engine/node-02-provisioning.json
```

两者必须不同。确认执行机已将 deviceSecret 写入操作系统凭据库且能够建立 Session 后，
安全销毁 provisioning 临时文件；企业 Secret 系统是否保留恢复副本按凭证托管策略决定。
数据库只保存 keyed hash，不保存原始 deviceSecret。

## 10. Agent 通用配置

每台执行机建立独立 `agent.env`。Node 01 示例：

```dotenv
CRAWLER_API_URL=http://crawler-api.internal:8080
AGENT_ID=REPLACE_WITH_NODE_01_AGENT_UUID
AGENT_DEVICE_SECRET_KEYRING_SERVICE=job-collection-agent
AGENT_DEVICE_SECRET_KEYRING_USERNAME=REPLACE_WITH_NODE_01_AGENT_UUID
AGENT_DATA_DIR=REPLACE_WITH_ABSOLUTE_NODE_LOCAL_DATA_PATH
BROWSER_PROFILE_ROOT=REPLACE_WITH_ABSOLUTE_NODE_LOCAL_PROFILE_PATH
AGENT_HEARTBEAT_SECONDS=20
TASK_LEASE_RENEW_SECONDS=30
BROWSER_NAME=chrome
BROWSER_PROFILE_KEY=zhaopin-primary
SUPPORTED_SOURCES=["zhaopin"]
CLAIM_POLL_SECONDS=5
PROFILE_LOCK_TIMEOUT_SECONDS=5
BROWSER_SHUTDOWN_TIMEOUT_SECONDS=10
SPOOL_BATCH_SIZE=100
TRANSPORT_MAX_ATTEMPTS=3
TRANSPORT_RETRY_BASE_SECONDS=0.5
```

Node 02 使用自己的 agentId、数据目录和 Profile 根目录。`BROWSER_PROFILE_KEY` 可以同名，
因为 Profile 归属 Agent；本地物理目录绝不能共享。配置中禁止出现
`AGENT_DEVICE_SECRET=`。若控制中心前置 TLS，应把 URL 改为企业证书可信的 HTTPS 地址。

## 11. Linux 执行机安装

### 11.1 安装运行环境

由管理员创建专用桌面账号并安装 Python 3.12、Chrome、图形桌面和 Secret Service。
Python 环境可以安装到 `/opt/job-collection-agent/venv`，但最终运行账号必须能够执行且
不能修改受控制品。示例：

```bash
python3.12 -m venv /opt/job-collection-agent/venv
/opt/job-collection-agent/venv/bin/python -m pip install --no-index \
  --find-links /opt/job-collection-agent/wheelhouse \
  crawler-contracts==0.1.0 desktop-collection-agent==0.1.0
```

随后以最终专用桌面账号登录图形会话。凭据、配置预检和托管安装都必须由该账号执行，
不得使用 root：

```bash
/opt/job-collection-agent/venv/bin/desktop-collection-agent-credential set \
  --service job-collection-agent \
  --username REPLACE_WITH_NODE_AGENT_UUID
/opt/job-collection-agent/venv/bin/desktop-collection-agent-credential status \
  --service job-collection-agent \
  --username REPLACE_WITH_NODE_AGENT_UUID
/opt/job-collection-agent/venv/bin/desktop-collection-agent-check-config \
  --config /absolute/path/agent.env
bash /srv/job-collection-engine/deploy/agent/linux/install.sh \
  --executable /opt/job-collection-agent/venv/bin/desktop-collection-agent \
  --config /absolute/path/agent.env
```

`credential set` 使用隐藏输入并二次确认。Secret Service 必须在当前图形会话解锁。安装器
创建 systemd user service 和桌面 autostart，不启用 lingering，不安装系统级 daemon。

检查：

```bash
systemctl --user status desktop-collection-agent.service
journalctl --user -u desktop-collection-agent.service --since "10 minutes ago"
```

日志中不得出现 deviceSecret、Session Token、lease Token、Cookie 或页面源码。

## 12. Windows 执行机安装

以下命令全部由最终运行 Agent 的专用交互桌面账号执行。不要在远程非交互 Session、
SYSTEM、Windows Service 或计划任务 Session 0 中安装。先把 ZIP 放到
`C:\JobCollectionAgent\staging`，校验外层制品并解压：

```powershell
$Package = 'C:\JobCollectionAgent\staging\desktop-agent-windows-x86_64-0.1.0.zip'
$ExpectedHash = '2916f9711cbd4498965430a760e4532e2643dbc97fd6bd6388eff439ea312729'
$ActualHash = (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash.ToLower()
if ($ActualHash -ne $ExpectedHash) { throw "Agent package SHA-256 mismatch: $ActualHash" }
Expand-Archive -LiteralPath $Package -DestinationPath 'C:\JobCollectionAgent\release' -Force
```

验证包内所有文件。任何 mismatch 都必须重新取得制品，不得继续安装：

```powershell
$Release = 'C:\JobCollectionAgent\release'
Get-Content "$Release\SHA256SUMS" | ForEach-Object { if ($_ -match '^([0-9a-f]{64})\s+(.+)$') { $Expected = $Matches[1]; $Relative = $Matches[2] -replace '^\./', ''; $Actual = (Get-FileHash -LiteralPath (Join-Path $Release $Relative) -Algorithm SHA256).Hash.ToLower(); if ($Actual -ne $Expected) { throw "Package member checksum mismatch: $Relative" } } }
Write-Output 'Agent package members verified'
```

确认系统安装了 64 位 Python 3.12、Chrome 或 Edge。建立隔离 venv 并从 wheelhouse 离线
安装，不允许命令回退到公网包源：

```powershell
py -3.12 -m venv C:\JobCollectionAgent\release\venv
C:\JobCollectionAgent\release\venv\Scripts\python.exe -m pip install --no-index --find-links C:\JobCollectionAgent\release\wheelhouse crawler-contracts==0.1.0 desktop-collection-agent==0.1.0
C:\JobCollectionAgent\release\venv\Scripts\python.exe -m pip check
```

从 `examples\desktop-agent-node-01.env.example` 建立
`C:\JobCollectionAgent\release\agent-node-01.env`。Windows 路径不要加引号，不要在行尾加
分号；`SUPPORTED_SOURCES=["zhaopin"]` 必须是合法 JSON。Node 01 的数据和 Profile 目录
例如：

```dotenv
AGENT_DATA_DIR=C:\JobCollectionAgent\release\data-node-01
BROWSER_PROFILE_ROOT=C:\JobCollectionAgent\release\profiles-node-01
```

写入 deviceSecret 时程序会隐藏输入并要求确认。以下命令均为单行：

```powershell
C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-credential.exe set --service job-collection-agent --username REPLACE_WITH_NODE_AGENT_UUID
C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-credential.exe status --service job-collection-agent --username REPLACE_WITH_NODE_AGENT_UUID
C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent-check-config.exe --config C:\JobCollectionAgent\release\agent-node-01.env
```

预检必须输出 `Agent configuration is valid` 后才安装计划任务。配置通过说明 keyring 中的
deviceSecret 可由当前用户读取，但不会打印 secret。

安装命令也保持单行，不要复制 PowerShell 界面中的 `PS C:\>` 或 `>>`。`>>` 是续行提示
符，复制后会被解析成重复输出重定向并报 `StreamAlreadyRedirected`：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\install.ps1 -ExecutablePath C:\JobCollectionAgent\release\venv\Scripts\desktop-collection-agent.exe -ConfigPath C:\JobCollectionAgent\release\agent-node-01.env
```

若 `Register-ScheduledTask` 返回 `0x80070005`，保持当前专用 Agent 用户登录，以“管理员
身份运行”PowerShell 后重新执行上面的同一条命令。不能切换到另一个管理员账号执行，
因为安装器按当前 Windows identity 创建任务；切换账号会让 Agent 使用错误的 keyring 和
桌面 Session。任务本身仍以当前 Agent 用户、`Interactive`、`RunLevel Limited` 运行。

检查任务身份、状态和最近返回码：

```powershell
Get-ScheduledTask -TaskName 'Job Collection Desktop Agent' | Select-Object TaskName,State,@{Name='UserId';Expression={$_.Principal.UserId}},@{Name='LogonType';Expression={$_.Principal.LogonType}},@{Name='RunLevel';Expression={$_.Principal.RunLevel}}
Get-ScheduledTaskInfo -TaskName 'Job Collection Desktop Agent' | Format-List LastRunTime,LastTaskResult,NumberOfMissedRuns
```

任务持续运行时 `LastTaskResult` 可能显示十进制 `267009`（`0x41301`，Task is currently
running），不能误判为失败。任务状态应为 `Running`，UserId 必须是专用 Agent 用户。
同时从执行机验证 API：

```powershell
Invoke-RestMethod -Uri 'http://10.100.11.51:8080/health/live' -Method Get
Invoke-RestMethod -Uri 'http://10.100.11.51:8080/health/ready' -Method Get
```

当前代码通过 Selenium Manager 发现 Chrome/Edge driver。生产网络不允许下载时，必须由
最终 Agent 用户预热 Selenium 缓存，或通过企业软件分发安装与浏览器匹配且可发现的
driver。没有可用 driver 时不能降级为 FakeDriver。

正常启停只能使用配套脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\stop.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\JobCollectionAgent\release\agent\windows\start.ps1
```

## 13. Agent 首次上线检查

先只启动 Node 01，Node 02 保持停止。Node 01 应在没有 Job 时完成 Session、heartbeat 和
空 claim 轮询，但不会启动浏览器。数据库仅查询非敏感状态；以下 SQL 在生产容器内或
外部数据库受控终端执行：

```sql
SELECT id, display_name, status, heartbeat_at, capabilities, current_task_id
  FROM crawler.collection_agents
 ORDER BY display_name;

SELECT agent_id, source, profile_key, status, updated_at
  FROM crawler.browser_profiles
 ORDER BY agent_id, source, profile_key;
```

Node 01 准入条件：

- status 在线且 heartbeat 持续更新；
- capabilities 的 `supportedSources` 为 `zhaopin`，最大并发槽为 1；
- heartbeat 建立 `zhaopin-primary` Profile 状态记录；
- 无 Job 时没有浏览器和租约；
- 关闭图形会话后不能继续启动可见浏览器任务。

Node 01 完成一次真实单节点闭环后再启动 Node 02，重复相同检查。不要同时引入两个未验证
节点，否则身份、浏览器和网络故障难以定位。

## 14. 无上游应用时申请 Caller Token

以下步骤在受控验收终端执行。它调用正式 API，不写数据库。先准备内存临时目录和清理
钩子：

```bash
umask 077
E2E_WORK_DIR="$(mktemp -d /dev/shm/job-collection-e2e.XXXXXX)"
E2E_TENANT_KEY_FILE=/secure/path/e2e-tenant.key
E2E_API_BASE=http://crawler-api.internal:8080
trap 'rm -rf -- "$E2E_WORK_DIR"' EXIT
```

创建 Token 请求时从文件读取 tenantKey，不把值放入命令行：

```bash
jq -n --rawfile tenantKey "$E2E_TENANT_KEY_FILE" \
  '{tenantKey: ($tenantKey | gsub("[\\r\\n]+$"; ""))}' \
  > "$E2E_WORK_DIR/token-request.json"
curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  --data-binary "@$E2E_WORK_DIR/token-request.json" \
  "$E2E_API_BASE/v1/auth/token" \
  > "$E2E_WORK_DIR/token-response.json"
jq '{tokenType, expiresIn, scopes}' "$E2E_WORK_DIR/token-response.json"
```

响应应为 Bearer、约 3600 秒，并具有三个 collection scope。为避免 Token 出现在 curl 进程
参数中，写入权限 0600 的临时 curl 配置：

```bash
CALLER_TOKEN="$(jq -er .accessToken "$E2E_WORK_DIR/token-response.json")"
printf 'header = "Authorization: Bearer %s"\n' "$CALLER_TOKEN" \
  > "$E2E_WORK_DIR/curl-auth.conf"
unset CALLER_TOKEN
```

后续 curl 统一使用 `--config "$E2E_WORK_DIR/curl-auth.conf"`。不要开启 shell xtrace，不要
打印 token-response 或 curl-auth 文件。会话退出时 trap 会删除内存临时文件。

## 15. 直接下发真实采集 Job

### 15.1 单节点基本闭环

生成可追踪但不包含业务身份的 externalRequestId。其协议格式为 1 至 160 位，仅允许
字母、数字、点、下划线、冒号和连字符，并且首位必须是字母或数字：

```bash
E2E_RUN_ID="e2e-$(date +%Y%m%dT%H%M%S)-$(openssl rand -hex 4)"
E2E_IDEMPOTENCY_KEY="$(openssl rand -hex 32)"
```

创建不带 callback 的真实 Job 请求：

```bash
jq -n --arg requestId "$E2E_RUN_ID" '
  {
    externalRequestId: $requestId,
    schemaVersion: 1,
    source: "zhaopin",
    queries: [
      {keyword: "Python", region: "北京", pageLimit: 2}
    ],
    collectionMode: "full",
    metadata: {purpose: "e2e-acceptance", phase: "single-node"}
  }
' > "$E2E_WORK_DIR/job-request.json"
curl --fail-with-body --silent --show-error \
  --config "$E2E_WORK_DIR/curl-auth.conf" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $E2E_IDEMPOTENCY_KEY" \
  --data-binary "@$E2E_WORK_DIR/job-request.json" \
  "$E2E_API_BASE/v1/collection-jobs" \
  > "$E2E_WORK_DIR/job-response.json"
E2E_JOB_ID="$(jq -er .collectionJobId "$E2E_WORK_DIR/job-response.json")"
jq . "$E2E_WORK_DIR/job-response.json"
unset E2E_IDEMPOTENCY_KEY
```

预期 HTTP 202，初始状态为 `queued`。相同 Idempotency-Key 只能与完全相同的请求重放；
相同 key 或同一 tenant 下相同 externalRequestId 搭配不同 payload 会得到 409，不能遇到
冲突后直接修改数据库。

Node 01 随后应领取 Task、打开可见浏览器并使用持久化 Profile。若出现登录或安全验证，
由专用桌面账号的操作人员在同一窗口完成。不得导出 Cookie、复制 Profile 或新开一个
绕过 Agent 监督的浏览器。

### 15.2 查询状态、结果和事件

```bash
curl --fail-with-body --silent --show-error \
  --config "$E2E_WORK_DIR/curl-auth.conf" \
  "$E2E_API_BASE/v1/collection-jobs/$E2E_JOB_ID" \
  > "$E2E_WORK_DIR/job-status.json"
jq '{collectionJobId, desiredState, status, taskStatusCounts, processedCount,
     failureCodeCounts, createdAt, updatedAt, completedAt}' \
  "$E2E_WORK_DIR/job-status.json"

curl --fail-with-body --silent --show-error \
  --config "$E2E_WORK_DIR/curl-auth.conf" \
  "$E2E_API_BASE/v1/collection-jobs/$E2E_JOB_ID/records?limit=50" \
  > "$E2E_WORK_DIR/records-page-1.json"
jq '{count: (.items | length), nextCursor}' \
  "$E2E_WORK_DIR/records-page-1.json"

curl --fail-with-body --silent --show-error \
  --config "$E2E_WORK_DIR/curl-auth.conf" \
  "$E2E_API_BASE/v1/collection-jobs/$E2E_JOB_ID/events?limit=50" \
  > "$E2E_WORK_DIR/events-page-1.json"
jq '[.items[] | {eventId, eventType, occurredAt, jobVersion}]' \
  "$E2E_WORK_DIR/events-page-1.json"
```

若 `nextCursor` 非空，使用原响应的值作为下一次 `cursor`，并保持相同 `limit`。不要自行
解析或修改 opaque cursor。当前事件类型应只来自：

- `collection.job.accepted`
- `collection.job.desired_state_changed`
- `collection.job.terminal`

通过门槛是 Job 到达 `succeeded` 或经评审可接受的 `partially_succeeded`，存在真实解析
记录，并且 Agent 释放浏览器、Profile 锁、Task lease 和 `current_task_id`。失败必须保留
`failureCodeCounts` 和时间线，不能改写为通过。

## 16. 暂停、恢复和取消验证

控制 API 为：

```text
POST /v1/collection-jobs/{jobId}/pause
POST /v1/collection-jobs/{jobId}/resume
POST /v1/collection-jobs/{jobId}/cancel
```

为暂停/恢复创建一个 `pageLimit` 足以留出操作窗口的新 Job，等待状态变成 `running` 后：

```bash
curl --fail-with-body --silent --show-error \
  --config "$E2E_WORK_DIR/curl-auth.conf" \
  -X POST "$E2E_API_BASE/v1/collection-jobs/$E2E_JOB_ID/pause" \
  > "$E2E_WORK_DIR/pause-response.json"
jq '{desiredState, status, version, taskStatusCounts}' \
  "$E2E_WORK_DIR/pause-response.json"
```

Agent 只会在 Adapter 安全点响应控制：开始处理一个详情页后会完成当前记录，在下一个
详情或页面安全点停止。确认状态到达 `paused`、checkpoint 和已接受记录稳定后：

```bash
curl --fail-with-body --silent --show-error \
  --config "$E2E_WORK_DIR/curl-auth.conf" \
  -X POST "$E2E_API_BASE/v1/collection-jobs/$E2E_JOB_ID/resume" \
  > "$E2E_WORK_DIR/resume-response.json"
jq '{desiredState, status, version, taskStatusCounts}' \
  "$E2E_WORK_DIR/resume-response.json"
```

取消必须使用另一个新 Job 验证。等待 running 后调用 `/cancel`，确认不再产生新导航和新
记录，最终为 `cancelled`。对终态 Job 发出不合法转换应返回 409；不能把 409 当成控制
成功。若 Job 太快结束而没有观察到控制生效，应以新的 externalRequestId 和
Idempotency-Key 重跑，不能伪造中间状态。

## 17. 双节点、失联接管和 spool 恢复

只有 Node 01 单节点闭环通过后才启动 Node 02。创建至少包含两个 query 的 Job，使控制面
生成至少两个 Task：

```json
{
  "externalRequestId": "e2e-YYYYMMDDTHHMMSS-dual-node",
  "schemaVersion": 1,
  "source": "zhaopin",
  "queries": [
    {"keyword": "Python", "region": "北京", "pageLimit": 3},
    {"keyword": "Java", "region": "北京", "pageLimit": 3}
  ],
  "collectionMode": "full",
  "metadata": {"purpose": "e2e-acceptance", "phase": "dual-node"}
}
```

确认两个不同 agentId 各领取一个 Task，且没有两个活动租约指向同一个 Task。随后按
[双执行机验收场景](../docs/acceptance-8.4.md) 执行：

1. Node 01 持有租约时停止其 Agent 或隔离 API 网络；
2. 等待 heartbeat 离线阈值和租约过期，不手工修改数据库；
3. Node 02 接管可恢复 Task，产生更大的 fencing token；
4. 证明旧 lease 的迟到写入返回 409，不能覆盖新 attempt；
5. 在记录上传前断开 API 或终止 Agent，恢复后确认 SQLite spool 重放；
6. 同一 batch 重放不产生重复 parsed record；
7. Task 终态后确认已确认 spool 和 checkpoint 按协议清理。

故障注入必须由变更窗口批准，并记录准确开始/恢复时间。不要删除 spool、Profile 锁、
数据库租约或 attempt 来“帮助”接管。Windows 使用配套 stop marker 脚本，Linux 使用
systemd user service；强杀只能作为专门的崩溃场景并保留证据。

## 18. 数据库和运行证据核对

以下查询不读取凭证 hash、callback ciphertext 或岗位描述正文，可用于脱敏证据：

```sql
SELECT id, display_name, status, heartbeat_at, current_task_id
  FROM crawler.collection_agents
 ORDER BY display_name;

SELECT id, external_request_id, desired_state, status, version,
       created_at, updated_at, completed_at
  FROM crawler.collection_jobs
 WHERE external_request_id LIKE 'e2e-%'
 ORDER BY created_at;

SELECT id, collection_job_id, status, leased_agent_id, fencing_token,
       attempt_count, processed_count, failure_code, lease_expires_at
  FROM crawler.collection_tasks
 WHERE collection_job_id = 'REPLACE_WITH_JOB_UUID';

SELECT collection_task_id, agent_id, fencing_token, claimed_at,
       released_at, outcome, failure_code
  FROM crawler.collection_task_attempts
 WHERE collection_task_id IN (
       SELECT id FROM crawler.collection_tasks
        WHERE collection_job_id = 'REPLACE_WITH_JOB_UUID')
 ORDER BY claimed_at;

SELECT event_type, occurred_at
  FROM crawler.collection_events
 WHERE collection_job_id = 'REPLACE_WITH_JOB_UUID'
 ORDER BY occurred_at;

SELECT count(*) AS parsed_record_count
  FROM crawler.parsed_job_records
 WHERE collection_task_id IN (
       SELECT id FROM crawler.collection_tasks
        WHERE collection_job_id = 'REPLACE_WITH_JOB_UUID');
```

验收证据至少包括：

- release commit、API 镜像 ID、PostgreSQL image digest 和 Alembic revision；
- 主机/容器/数据库时区和时间同步结果；
- health/live、health/ready 的时间戳和响应摘要；
- 两台 Agent 的 agentId、能力、heartbeat 与 Profile 状态，不含 deviceSecret；
- 每个场景的 externalRequestId、Job/Task/attempt 时间线和 API 状态码；
- 浏览器可见、人工登录/验证、进程树和 Profile 锁的实机证据；
- pause/resume/cancel 的事件版本和安全点结果；
- 失联接管、fencing 409、spool 重放和批次去重证据；
- 脱敏数据库快照、备份校验与恢复演练记录；
- 敏感信息扫描报告。

岗位结果属于原始采集数据，验收报告默认只记录数量、ID 和 hash，不复制完整岗位描述。

## 19. 敏感信息检查

以下内容不得出现在日志、命令行记录、截图、数据库快照或报告中：

- PostgreSQL 密码；
- tenantKey；
- deviceSecret；
- Caller Token、Agent Session Token、lease Token、Callback Token；
- Cookie、HTML、截图内容或浏览器 Profile 文件。

对证据目录执行只返回文件名、不打印命中内容的检查：

```bash
rg -l -i \
  'Bearer [A-Za-z0-9._~-]{20,}|"(deviceSecret|accessToken|sessionToken|leaseToken)"[[:space:]]*:[[:space:]]*"[^" ]+' \
  /var/log/job-collection-evidence
```

任何命中都应隔离原文件、吊销或轮换相关凭证，并生成脱敏替代证据。不能把 Secret 替换
为星号后继续保留可恢复的原始副本在普通日志目录。还应使用组织批准的 DLP/Secret
扫描工具覆盖二进制、压缩包和截图；上述 `rg` 只能作为最低限度文本检查。

## 20. 监控和故障判定

验证窗口至少监控：

- API 进程存活、readiness、5xx/401/409 比例和请求耗时；
- PostgreSQL 连接数、锁等待、磁盘、WAL、备份状态和容器重启；
- Agent heartbeat 延迟、在线状态、claim、租约过期和 attempt 增长；
- Job queued/running/awaiting_human/terminal 数量；
- parsed record 写入量、spool pending 批次数；
- 浏览器/WebDriver 进程、Profile 锁和执行机磁盘。

以下任一情况都应停止继续扩展场景并进入故障处理：

- migration 非 head、readiness 503 或数据库时间/版本不符；
- 任一 Secret 出现在普通日志或报告；
- 两个 Agent 共用 agentId、deviceSecret、数据目录或 Profile；
- Agent 绑定或接收 tenant 信息，或能够直连数据库；
- 无图形会话仍宣称人工登录/真实浏览器验收成功；
- 使用 FakeDriver、离线页面或测试 Actor 代替真实来源；
- 旧 fencing 写入被接受、重复记录无法幂等去重；
- 进程退出后 Profile 锁或浏览器进程未按协议释放且原因未查明。

## 21. 发布回滚、停机和卸载

### 21.1 API 发布回滚

1. 停止下发新 Job，等待活动 Task 到安全点或通过 API 取消；
2. 备份数据库并记录当前 Alembic revision；
3. 保留当前和上一版 release commit、镜像 ID及配置；
4. 若 schema 向后兼容，只回滚 API 制品并重新验证 readiness；
5. 不执行未经演练的 `alembic downgrade`；
6. schema 不兼容时，按批准的恢复方案在停机窗口恢复数据库备份；
7. 回滚后重新核对 tenant 同步、Agent Session、heartbeat 和一个受控 Job。

当前 Compose 从本地 release 构建镜像，没有远程不可变 image digest 配置。正式生产发布
前应将构建结果推送到受控制品仓库并以 digest 固定，或至少保留经校验的上一版镜像和
release checkout；否则“重新构建上一 commit”仍受基础镜像变化影响。

### 21.2 Agent 停机和卸载

Agent 必须先协作停止，确认当前 Task、spool、浏览器进程和 Profile 锁状态，再执行
[Agent RUNBOOK](agent/RUNBOOK.md) 中的卸载脚本。卸载默认保留配置、spool 和 Profile，
避免丢失尚未确认记录或登录状态。

确认不再恢复该节点后，在控制中心执行：

```bash
docker compose \
  --env-file "$CONTROL_ENV" \
  --env-file "$CONTROL_KEYS" \
  -f "$CONTROL_COMPOSE" exec -T crawler-api \
  disable-agent --agent-id REPLACE_WITH_AGENT_UUID
```

该操作禁用 Agent 并吊销未过期 Session，但不会替代 Task 租约恢复。凭证吊销、数据恢复
确认和证据留存完成后，才能按数据保留策略清除本地 keyring、spool 和 Profile。

### 21.3 控制中心停机

正常停机不删除数据卷：

```bash
docker compose \
  --env-file "$CONTROL_ENV" \
  --env-file "$CONTROL_KEYS" \
  -f "$CONTROL_COMPOSE" stop crawler-api postgres
```

禁止使用 `docker compose down -v` 作为重装或回滚手段。任何数据库卷删除都属于独立的
破坏性数据处置，必须有已验证备份、双人确认和明确审批。

## 22. 最终验收门槛

环境准备完成不代表端到端验证通过。只有以下条件全部满足，才能把真实 E2E 标记为通过：

- 控制中心从干净环境部署，PostgreSQL 18.1、`Asia/Shanghai`、migration 0009 和
  readiness 均通过；
- 独立验收 tenant 通过正式认证接口获得 Caller Token，并通过 API 创建 Job；
- 两台不同执行机各自完成至少一次真实 Zhaopin 采集；
- 两台执行机均验证可见浏览器、人工登录或安全验证，以及 Profile 重启复用；
- pause、resume、cancel、双节点调度、租约接管和旧 fencing 拒绝通过；
- API 断连或 Agent 崩溃后的 spool 重放和批次幂等通过；
- Job 结果可通过 records/events API 读取，数据库只保存允许的运行元数据和解析记录；
- 备份已在隔离环境恢复；
- 敏感信息扫描无未处置命中；
- 每个结论都有可复核时间线、状态码、脱敏数据库快照和实机进程证据。

本轮最终报告必须明确写明“上游应用下发 Job 与 Webhook 回调未纳入验收”，不能用验收
操作终端直接调用 API 的结果推导上游对接已经完成。

## 附录 A：安装故障快速定位

| 现象 | 判定和处理 |
|---|---|
| Docker bind mount 报 `Source must not be empty` | shell 变量未定义。改用已确认存在的绝对路径，或重新设置 `CONTROL_ALLOWLIST` 后执行；不要提交空 `src=`。 |
| allowlist 加载报 `PermissionError: [Errno 13]` | bind 文件对容器内非 root 用户不可读。将只含 fingerprint 的 allowlist 设为 `root:root 0644`，保持密钥文件 `0600`，不要把容器改成 root。 |
| `tenant allowlist is missing or invalid` | 先用 7.2 节单行 `docker run ... python -c` 区分挂载、权限和 YAML schema。确认键名为 `tenantId`、`tenantKeyHashes`、`callbackAllowlist`，hash 为 `sha256:` 加 64 位十六进制。 |
| `python -c` 报 `IndentationError` | 多行命令被 shell 保留了开头空格。使用手册中的单行 `python -c`，不要把排版缩进复制进代码字符串。 |
| Agent 预检报 `invalid Agent configuration` | 检查每行只有一个 `KEY=value`，行尾没有分号；`SUPPORTED_SOURCES=["zhaopin"]` 是 JSON；Windows 数据目录和 Profile 目录是绝对路径；keyring service/username 与写入凭据时一致。 |
| PowerShell 报 `IsPathFullyQualified` 不存在 | 使用修复后的 0.1.0 ZIP，外层 SHA-256 必须为本手册记录值；旧 `install.ps1` 不兼容 Windows PowerShell 5.1。 |
| PowerShell 报 `StreamAlreadyRedirected` | 把交互提示符 `>>` 复制成了命令。按 12 节使用单行命令，不复制 `PS C:\>` 或 `>>`。 |
| `Register-ScheduledTask` 报拒绝访问/`0x80070005` | 在同一个专用 Agent 用户会话中以管理员身份打开 PowerShell，再执行安装命令。不得用另一个管理员账号代跑。 |
| 计划任务 `LastTaskResult=267009` | 十六进制为 `0x41301`，表示任务仍在运行。结合 `State=Running`、heartbeat 和 API 日志判断，不把该值单独当成失败。 |
| API `/health/live` 正常但 `/health/ready` 为 503 | 进程存活但数据库不可用。检查 external DB 网络、账号权限、SSL mode、migration 日志和 `alembic current`，不要只重启 API。 |
| Agent 已运行但控制面无 heartbeat | 依次检查执行机到 API 的网络、系统时间、AgentId、同一 Windows 用户的 keyring、Agent Session API 日志；禁止重新预置身份覆盖原 deviceSecret。 |
| Agent heartbeat 正常但 Profile 不可用 | 检查 Profile 目录权限、残留浏览器进程和本地锁登记。先协作停止 Agent，确认登记进程退出，再恢复；不要直接删除锁文件。 |

## 附录 B：安装完成检查清单

- API 镜像 tar 和 Agent ZIP 的 SHA-256 与受控制品记录一致；
- 控制中心使用 `--no-build` 启动，镜像 ID 与构建报告一致；
- migration 为 `0009 (head)`，数据库版本为 PostgreSQL 18，时区为 `Asia/Shanghai`；
- `/health/live` 和 `/health/ready` 都返回 `{"status":"ok"}`；
- allowlist tenant 已同步到 `crawler.api_clients`，原始 tenantKey 未进入数据库和日志；
- 每台 Agent 使用不同的 agentId、deviceSecret、数据目录和 Profile 目录；
- Windows 计划任务属于专用交互用户，LogonType 为 Interactive，Agent heartbeat 持续更新；
- `zhaopin-primary` Profile 已登记，Agent 空闲时没有 Task lease 和浏览器进程；
- Node 01 单节点闭环通过后才启用 Node 02；
- 本轮报告明确排除上游应用下发和 Webhook 回调验收。
