from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .api.v1.agent_heartbeat import router as agent_heartbeat_router
from .api.v1.agent_sessions import router as agent_sessions_router
from .api.v1.agent_tasks import router as agent_tasks_router
from .api.v1.caller_auth import router as caller_auth_router
from .api.v1.collection_jobs import router as collection_jobs_router
from .api.v1.health import router as health_router
from .api.v1.internal_metrics import router as internal_metrics_router
from .api.validation_errors import install_validation_error_handler
from .application.access_tokens import AccessTokenIssuer
from .application.agent_authentication import AgentSessionAuthenticator
from .application.agent_heartbeats import AgentHeartbeatRecorder
from .application.agent_sessions import AgentSessionIssuer
from .application.agent_task_claims import AgentTaskClaimer
from .application.agent_task_runtime import AgentTaskRuntime, ExpiredLeaseRecoverer
from .application.callback_token_rotation import CollectionJobCallbackTokenRotator
from .application.caller_authentication import CallerAccessTokenAuthenticator
from .application.collection_events import CollectionEventReader
from .application.collection_jobs import (
    CollectionJobController,
    CollectionJobReader,
    CollectionJobSubmitter,
)
from .application.parsed_records import ParsedRecordBatchIngestor, ParsedRecordReader
from .application.ports import UnitOfWork
from .application.task_leases import ActiveTaskLeaseAuthenticator
from .application.tenant_allowlist import TenantAllowlistSynchronizer
from .application.webhook_dispatcher import WebhookDispatcher, WebhookDispatcherRunner
from .application.webhook_operations import WebhookDeliveryOperator
from .config import get_settings
from .infrastructure.database.session import create_database_engine, create_session_factory
from .infrastructure.database.unit_of_work import create_unit_of_work
from .infrastructure.observability import WebhookPrometheusMetrics
from .infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from .infrastructure.security.callback_tokens import CallbackTokenProtector
from .infrastructure.security.device_credentials import DeviceCredentialHasher
from .infrastructure.security.task_lease_tokens import TaskLeaseTokenManager
from .infrastructure.security.tenant_allowlist import load_tenant_allowlist
from .infrastructure.security.token_hashing import AccessTokenHasher
from .infrastructure.webhooks.http import HttpxWebhookTransport


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    def unit_of_work_factory() -> UnitOfWork:
        return create_unit_of_work(session_factory)

    webhook_transport: HttpxWebhookTransport | None = None
    webhook_runner: WebhookDispatcherRunner | None = None
    try:
        allowlist = load_tenant_allowlist(settings.tenant_allowlist_path)
        synchronizer = TenantAllowlistSynchronizer(unit_of_work_factory)
        app.state.tenant_allowlist_sync_result = await synchronizer.synchronize(allowlist)
        app.state.tenant_allowlist = allowlist
        app.state.database_engine = engine
        app.state.session_factory = session_factory
        token_hasher = AccessTokenHasher(settings.api_token_hash_key)
        app.state.access_token_issuer = AccessTokenIssuer(
            allowlist=allowlist,
            unit_of_work_factory=unit_of_work_factory,
            hash_token=token_hasher.hash,
            ttl_seconds=settings.api_access_token_ttl_seconds,
        )
        caller_authenticator = CallerAccessTokenAuthenticator(token_hasher.hash)

        callback_token_protector = CallbackTokenProtector(
            encryption_key=settings.callback_token_encryption_key,
            fingerprint_key=settings.callback_token_fingerprint_key,
            encryption_key_version=settings.callback_token_encryption_key_version,
        )
        webhook_metrics: WebhookPrometheusMetrics | None = None
        if settings.webhook_metrics_enabled:
            webhook_metrics = WebhookPrometheusMetrics(
                unit_of_work_factory=unit_of_work_factory,
            )
            app.state.webhook_metrics = webhook_metrics
        app.state.collection_job_submitter = CollectionJobSubmitter(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=caller_authenticator,
            tenant_allowlist=allowlist,
            callback_token_protector=callback_token_protector,
        )
        app.state.collection_job_reader = CollectionJobReader(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=caller_authenticator,
        )
        app.state.collection_job_controller = CollectionJobController(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=caller_authenticator,
        )
        app.state.collection_job_callback_token_rotator = CollectionJobCallbackTokenRotator(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=caller_authenticator,
            callback_token_protector=callback_token_protector,
        )
        device_credential_hasher = DeviceCredentialHasher(settings.agent_device_credential_hash_key)
        agent_session_token_hasher = AgentSessionTokenHasher(settings.agent_token_hash_key)
        agent_authenticator = AgentSessionAuthenticator(agent_session_token_hasher.hash)
        app.state.agent_session_issuer = AgentSessionIssuer(
            unit_of_work_factory=unit_of_work_factory,
            verify_device_secret=device_credential_hasher.verify,
            hash_session_token=agent_session_token_hasher.hash,
            ttl_seconds=settings.agent_session_ttl_seconds,
        )
        app.state.agent_heartbeat_recorder = AgentHeartbeatRecorder(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=agent_authenticator,
            profile_offline_seconds=settings.agent_offline_seconds,
        )
        lease_tokens = TaskLeaseTokenManager(settings.agent_token_hash_key)
        lease_authenticator = ActiveTaskLeaseAuthenticator(
            authenticator=agent_authenticator,
            lease_tokens=lease_tokens,
        )
        expired_lease_recoverer = ExpiredLeaseRecoverer(
            unit_of_work_factory=unit_of_work_factory,
        )
        app.state.agent_task_runtime = AgentTaskRuntime(
            unit_of_work_factory=unit_of_work_factory,
            lease_authenticator=lease_authenticator,
            lease_seconds=settings.lease_seconds,
        )
        app.state.parsed_record_batch_ingestor = ParsedRecordBatchIngestor(
            unit_of_work_factory=unit_of_work_factory,
            lease_authenticator=lease_authenticator,
        )
        app.state.parsed_record_reader = ParsedRecordReader(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=caller_authenticator,
        )
        app.state.collection_event_reader = CollectionEventReader(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=caller_authenticator,
        )
        app.state.webhook_delivery_operator = WebhookDeliveryOperator(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=caller_authenticator,
        )
        app.state.expired_lease_recoverer = expired_lease_recoverer
        app.state.agent_task_claimer = AgentTaskClaimer(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=agent_authenticator,
            lease_tokens=lease_tokens,
            lease_seconds=settings.lease_seconds,
            agent_offline_seconds=settings.agent_offline_seconds,
            recover_expired_lease=expired_lease_recoverer.recover_one,
        )
        if settings.webhook_dispatcher_enabled:
            webhook_transport = HttpxWebhookTransport(
                timeout_seconds=settings.webhook_request_timeout_seconds,
                max_connections=settings.webhook_dispatcher_concurrency,
            )
            webhook_dispatcher = WebhookDispatcher(
                unit_of_work_factory=unit_of_work_factory,
                callback_token_protector=callback_token_protector,
                transport=webhook_transport,
                claim_lease_seconds=settings.webhook_claim_lease_seconds,
                max_attempts=settings.webhook_max_attempts,
                retry_base_seconds=settings.webhook_retry_base_seconds,
                retry_max_seconds=settings.webhook_retry_max_seconds,
                retry_after_max_seconds=(settings.webhook_retry_after_max_seconds),
                telemetry=webhook_metrics,
            )
            webhook_runner = WebhookDispatcherRunner(
                webhook_dispatcher,
                concurrency=settings.webhook_dispatcher_concurrency,
                poll_interval_seconds=(settings.webhook_dispatcher_poll_interval_seconds),
                telemetry=webhook_metrics,
            )
            app.state.webhook_dispatcher = webhook_dispatcher
            app.state.webhook_dispatcher_runner = webhook_runner
            webhook_runner.start()
        yield
    finally:
        if webhook_runner is not None:
            await webhook_runner.stop()
        if webhook_transport is not None:
            await webhook_transport.close()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Job Collection Engine API",
        version="0.2.0",
        description="Business-neutral control plane for desktop collection agents.",
        lifespan=lifespan,
    )
    install_validation_error_handler(app)
    app.include_router(health_router)
    app.include_router(internal_metrics_router)
    app.include_router(caller_auth_router)
    app.include_router(agent_sessions_router)
    app.include_router(agent_heartbeat_router)
    app.include_router(agent_tasks_router)
    app.include_router(collection_jobs_router)
    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "crawler_api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
