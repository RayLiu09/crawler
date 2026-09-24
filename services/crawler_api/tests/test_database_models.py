from crawler_api.infrastructure.database.models import Base
from sqlalchemy.orm import configure_mappers

EXPECTED_TABLES = {
    "agent_sessions",
    "api_access_tokens",
    "api_clients",
    "browser_profiles",
    "collection_agents",
    "collection_events",
    "collection_job_callback_credential_rotations",
    "collection_job_callback_configs",
    "collection_jobs",
    "collection_task_attempts",
    "collection_tasks",
    "parsed_job_records",
    "parsed_record_batches",
    "webhook_outbox",
    "webhook_outbox_replays",
}


def test_all_migration_tables_have_orm_mappings() -> None:
    configure_mappers()

    crawler_tables = {
        table.name for table in Base.metadata.tables.values() if table.schema == "crawler"
    }
    foreign_keys = {
        foreign_key for table in Base.metadata.tables.values() for foreign_key in table.foreign_keys
    }

    assert crawler_tables == EXPECTED_TABLES
    assert len(foreign_keys) == 19


def test_security_defaults_match_migration_defaults() -> None:
    api_clients = Base.metadata.tables["crawler.api_clients"]
    access_tokens = Base.metadata.tables["crawler.api_access_tokens"]
    agents = Base.metadata.tables["crawler.collection_agents"]

    allowed_scopes_default = str(api_clients.c.allowed_scopes.server_default.arg)
    capabilities_default = str(agents.c.capabilities.server_default.arg)

    assert "collection:control" in allowed_scopes_default
    assert access_tokens.c.scopes.server_default is None
    assert "schemaVersion" in capabilities_default
    assert "maxConcurrentTasks" in capabilities_default


def test_webhook_outbox_has_recoverable_dispatch_claim_columns() -> None:
    outbox = Base.metadata.tables["crawler.webhook_outbox"]

    assert {
        "dispatch_claim_id",
        "dispatch_claimed_at",
        "dispatch_lease_expires_at",
    } <= set(outbox.c.keys())
    assert "ck_webhook_outbox_dispatch_claim" in {
        constraint.name for constraint in outbox.constraints
    }
    assert {"replay_count", "last_replayed_at"} <= set(outbox.c.keys())


def test_webhook_replay_history_has_permanent_idempotency_constraints() -> None:
    replays = Base.metadata.tables["crawler.webhook_outbox_replays"]
    constraint_names = {constraint.name for constraint in replays.constraints}

    assert "uq_webhook_outbox_replays_outbox_key" in constraint_names
    assert "uq_webhook_outbox_replays_outbox_number" in constraint_names
    assert "ck_webhook_outbox_replays_key_hash" in constraint_names


def test_callback_credential_material_has_a_destroyed_state() -> None:
    callback = Base.metadata.tables["crawler.collection_job_callback_configs"]

    assert callback.c.token_ciphertext.nullable
    assert callback.c.token_nonce.nullable
    assert callback.c.credential_destroyed_at.nullable
    assert "ck_collection_job_callback_configs_credential_material" in {
        constraint.name for constraint in callback.constraints
    }
