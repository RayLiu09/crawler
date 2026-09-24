import argparse
import asyncio
import json
import sys
from typing import Literal, cast

from crawler_api.config import get_settings
from crawler_api.infrastructure.database.session import create_database_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

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
EXPECTED_CONSTRAINTS = {
    "ck_api_access_tokens_scopes_array",
    "ck_api_clients_allowed_scopes_array",
    "ck_browser_profiles_source",
    "ck_collection_agents_capabilities_v1",
    "ck_collection_jobs_external_request_id",
    "ck_collection_jobs_request_payload_v1",
    "ck_collection_job_callback_configs_auth_scheme",
    "ck_collection_job_callback_configs_ciphertext_length",
    "ck_collection_job_callback_configs_credential_material",
    "ck_collection_job_callback_configs_credential_version",
    "ck_collection_job_callback_configs_fingerprint",
    "ck_collection_job_callback_configs_key_version",
    "ck_collection_job_callback_configs_nonce_length",
    "ck_callback_credential_rotations_key_hash",
    "ck_callback_credential_rotations_request_fingerprint",
    "ck_callback_credential_rotations_result_version",
    "ck_webhook_outbox_status",
    "ck_webhook_outbox_dispatch_claim",
    "ck_webhook_outbox_replay_count",
    "ck_webhook_outbox_replays_key_hash",
    "ck_webhook_outbox_replays_number",
    "ck_webhook_outbox_replays_previous_attempt_count",
    "ck_collection_tasks_source",
    "fk_webhook_outbox_callback_config",
    "uq_collection_jobs_client_external_request",
    "uq_collection_jobs_client_idempotency",
    "uq_callback_credential_rotations_config_key",
    "uq_webhook_outbox_event_callback",
    "uq_webhook_outbox_replays_outbox_key",
    "uq_webhook_outbox_replays_outbox_number",
    "fk_collection_tasks_leased_profile",
}
EXPECTED_INDEXES = {
    "ix_agent_sessions_agent_expiry",
    "ix_api_access_tokens_client_expiry",
    "ix_collection_events_job_occurred",
    "ix_collection_tasks_status_lease_expiry",
    "ix_parsed_job_records_task_collected_id",
    "ix_parsed_job_records_task_created_id",
    "ix_webhook_outbox_pending",
    "ix_webhook_outbox_status_created",
    "uq_collection_tasks_active_profile",
}


async def relation_exists(connection: AsyncConnection, qualified_name: str) -> bool:
    return bool(
        await connection.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": qualified_name}
        )
    )


async def inspect_schema() -> dict[str, object]:
    engine = create_database_engine(get_settings())
    try:
        async with engine.connect() as connection:
            schema_exists = bool(
                await connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'crawler')")
                )
            )
            tables = list(
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname = 'crawler' ORDER BY tablename"
                        )
                    )
                ).scalars()
            )
            user_tables = [
                f"{row.schemaname}.{row.tablename}"
                for row in (
                    await connection.execute(
                        text(
                            "SELECT schemaname, tablename FROM pg_tables "
                            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
                            "ORDER BY schemaname, tablename"
                        )
                    )
                )
            ]

            row_counts: dict[str, int] = {}
            for table_name in tables:
                quoted_name = table_name.replace('"', '""')
                count = await connection.scalar(
                    text(f'SELECT count(*) FROM crawler."{quoted_name}"')
                )
                row_counts[table_name] = int(count or 0)

            alembic_revision: str | None = None
            if await relation_exists(connection, "public.alembic_version"):
                alembic_revision = await connection.scalar(
                    text("SELECT version_num FROM public.alembic_version LIMIT 1")
                )

            constraint_names = set(
                (
                    await connection.execute(
                        text(
                            "SELECT c.conname "
                            "FROM pg_constraint c "
                            "JOIN pg_class r ON r.oid = c.conrelid "
                            "JOIN pg_namespace n ON n.oid = r.relnamespace "
                            "WHERE n.nspname = 'crawler' ORDER BY c.conname"
                        )
                    )
                ).scalars()
            )
            foreign_key_count = int(
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_constraint c "
                        "JOIN pg_class r ON r.oid = c.conrelid "
                        "JOIN pg_namespace n ON n.oid = r.relnamespace "
                        "WHERE n.nspname = 'crawler' AND c.contype = 'f'"
                    )
                )
                or 0
            )
            indexes = set(
                (
                    await connection.execute(
                        text(
                            "SELECT indexname FROM pg_indexes "
                            "WHERE schemaname = 'crawler' ORDER BY indexname"
                        )
                    )
                ).scalars()
            )
            defaults = {
                row.column_name: row.column_default
                for row in (
                    await connection.execute(
                        text(
                            "SELECT table_name || '.' || column_name AS column_name, "
                            "column_default FROM information_schema.columns "
                            "WHERE table_schema = 'crawler' AND (table_name, column_name) IN "
                            "(('api_clients', 'allowed_scopes'), "
                            "('api_access_tokens', 'scopes'), "
                            "('collection_agents', 'capabilities')) "
                            "ORDER BY table_name, column_name"
                        )
                    )
                )
            }
            return {
                "schemaExists": schema_exists,
                "alembicRevision": alembic_revision,
                "tables": tables,
                "tableRowCounts": row_counts,
                "userTables": user_tables,
                "constraintCount": len(constraint_names),
                "criticalConstraintsPresent": sorted(EXPECTED_CONSTRAINTS & constraint_names),
                "foreignKeyCount": foreign_key_count,
                "criticalIndexesPresent": sorted(EXPECTED_INDEXES & indexes),
                "defaults": defaults,
            }
    finally:
        await engine.dispose()


def validate(result: dict[str, object], expected: Literal["empty", "migrated", "base"]) -> None:
    tables = set(cast(list[str], result["tables"]))
    if expected in {"empty", "base"}:
        if result["schemaExists"] or tables or result["alembicRevision"] is not None:
            raise ValueError(f"database is not in expected {expected} migration state")
        return

    missing_tables = EXPECTED_TABLES - tables
    missing_constraints = EXPECTED_CONSTRAINTS - set(
        cast(list[str], result["criticalConstraintsPresent"])
    )
    missing_indexes = EXPECTED_INDEXES - set(cast(list[str], result["criticalIndexesPresent"]))
    defaults = result["defaults"]
    if not isinstance(defaults, dict):
        raise ValueError("column defaults could not be inspected")
    allowed_scopes_default = str(defaults.get("api_clients.allowed_scopes", ""))
    capabilities_default = str(defaults.get("collection_agents.capabilities", ""))
    failures = {
        "revision": result["alembicRevision"] != "0009",
        "missingTables": sorted(missing_tables),
        "nonEmptyTables": result["tableRowCounts"] != {name: 0 for name in sorted(tables)},
        "missingConstraints": sorted(missing_constraints),
        "foreignKeyCount": result["foreignKeyCount"] != 19,
        "missingIndexes": sorted(missing_indexes),
        "allowedScopesDefault": "collection:control" not in allowed_scopes_default,
        "tokenScopesMustBeExplicit": defaults.get("api_access_tokens.scopes") is not None,
        "capabilitiesDefault": "schemaVersion" not in capabilities_default,
    }
    if any(failures.values()):
        raise ValueError(f"migrated schema verification failed: {failures}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the crawler schema without secrets.")
    parser.add_argument("--expect", choices=("empty", "migrated", "base"))
    args = parser.parse_args()
    try:
        result = asyncio.run(inspect_schema())
        if args.expect is not None:
            validate(result, args.expect)
    except Exception as error:
        print(
            json.dumps(
                {"status": "failed", "errorType": type(error).__name__, "message": str(error)},
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    result["status"] = "passed"
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
