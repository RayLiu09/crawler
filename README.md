# Job Collection Engine

Business-neutral job-demand data collection engine composed of two deployables:

- `services/crawler_api`: containerized control plane backed by external PostgreSQL.
- `agents/desktop_agent`: outbound-only desktop agent that owns browser automation.

The engine stores operational metadata and parsed source records only. It does not
store HTML, screenshots, cookies, browser profiles, cross-source canonical jobs,
or upstream business analysis results.

## Workspace

```text
contracts/               Shared Pydantic request and response contracts
services/crawler_api/    FastAPI control plane and PostgreSQL persistence
agents/desktop_agent/    Desktop execution agent and source adapters
docs/                    Architecture and implementation discussion documents
tests/                   Cross-component contract and end-to-end tests
deploy/                  Control-server Compose and execution-node examples
```

The API Docker build context is the `refactor` directory:

```bash
docker build -f services/crawler_api/Dockerfile -t crawler-api:dev .
```

Development reuses an existing PostgreSQL database. Production deploys PostgreSQL
as a separate container on the control-center server, without publishing port 5432,
and persists it on a dedicated volume. Database credentials are injected at
deployment time and must not be committed. The current platform is intentionally
not referenced or adapted by this project.

The execution plane is a horizontally scalable pool. Each desktop machine receives
its own `agentId` and `deviceSecret` during controlled installation; an Agent is not
paired with, or otherwise bound to, a tenant.

## Initial deployment assets

- `deploy/docker/docker-compose.dev.yml`: API and migration using the existing development DB.
- `deploy/docker/docker-compose.prod.yml`: API, migration, and production PostgreSQL container.
- `deploy/config/tenant-allowlist.example.yml`: read-only caller trust configuration.
- `deploy/examples/desktop-agent-node-01.env.example` and `node-02`: distinct Agent nodes.
- `services/crawler_api/src/crawler_api/infrastructure/database/migrations`: baseline schema.

Generate local initial hash/signing keys once (the output is ignored by Git):

```bash
python scripts/generate_initial_secrets.py
```

Run the local quality gate with `scripts/verify.sh`. Database admission and DBA
bootstrap instructions are recorded in `docs/implementation-status.md` and
`deploy/database/README.md`.
