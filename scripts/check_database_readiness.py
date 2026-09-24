import argparse
import asyncio
import json
import sys

from crawler_api.config import get_settings
from crawler_api.infrastructure.database.session import create_database_engine
from sqlalchemy import text


async def check(maintenance_database: str | None = None) -> dict[str, object]:
    settings = get_settings()
    target_database = settings.postgres_database
    if maintenance_database is not None:
        settings = settings.model_copy(update={"postgres_database": maintenance_database})
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            current_setting('server_version') AS server_version,
                            current_setting('TimeZone') AS session_timezone,
                            current_setting('max_connections')::integer AS max_connections,
                            current_database() AS database_name,
                            current_user AS database_user,
                            pg_is_in_recovery() AS in_recovery,
                            current_user IN (
                                SELECT rolname FROM pg_roles WHERE rolcreatedb
                            ) AS can_create_database,
                            EXISTS(
                                SELECT 1 FROM pg_database WHERE datname = :target_database
                            ) AS target_database_exists,
                            has_database_privilege(current_user, current_database(), 'CONNECT')
                                AS can_connect,
                            has_database_privilege(current_user, current_database(), 'CREATE')
                                AS can_create,
                            COALESCE(
                                (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()),
                                false
                            ) AS ssl_in_use
                        """
                    ),
                    {"target_database": target_database},
                )
            ).mappings().one()
            return dict(row)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only PostgreSQL readiness checks.")
    parser.add_argument(
        "--maintenance-database",
        help="Connect here while checking whether the configured target database exists.",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(check(args.maintenance_database))
    except Exception as error:
        print(
            json.dumps(
                {"status": "failed", "errorType": type(error).__name__},
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
