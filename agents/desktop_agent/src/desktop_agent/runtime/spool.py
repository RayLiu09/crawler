import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from crawler_contracts.parsed_jobs import ParsedJobRecord


@dataclass(frozen=True, slots=True)
class SpoolBatch:
    batch_id: UUID
    task_id: UUID
    records: list[ParsedJobRecord]
    created_at: datetime


class LocalSpool:
    """Durable local queue for records not yet acknowledged by the control API."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def enqueue(
        self, *, task_id: UUID, batch_id: UUID, records: list[ParsedJobRecord]
    ) -> None:
        if not records:
            raise ValueError("a spool batch must contain at least one record")
        payload = json.dumps(
            [record.model_dump(mode="json", by_alias=True) for record in records],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO spool_batches(batch_id, task_id, records_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(batch_id) DO NOTHING
                """,
                (str(batch_id), str(task_id), payload, datetime.now(UTC).isoformat()),
            )

    def pending(self, *, task_id: UUID | None = None) -> list[SpoolBatch]:
        query = (
            "SELECT batch_id, task_id, records_json, created_at "
            "FROM spool_batches WHERE acknowledged = 0"
        )
        parameters: tuple[str, ...] = ()
        if task_id is not None:
            query += " AND task_id = ?"
            parameters = (str(task_id),)
        query += " ORDER BY created_at, batch_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._batch_from_row(row) for row in rows]

    def acknowledge(self, batch_id: UUID) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE spool_batches SET acknowledged = 1 WHERE batch_id = ?",
                (str(batch_id),),
            )

    def save_checkpoint(self, *, task_id: UUID, page: int, processed_count: int) -> None:
        if page < 0 or processed_count < 0:
            raise ValueError("checkpoint values cannot be negative")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_checkpoints(task_id, page, processed_count)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    page = MAX(task_checkpoints.page, excluded.page),
                    processed_count = MAX(
                        task_checkpoints.processed_count, excluded.processed_count
                    )
                """,
                (str(task_id), page, processed_count),
            )

    def checkpoint(self, task_id: UUID) -> tuple[int, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT page, processed_count FROM task_checkpoints WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def clear_task(self, task_id: UUID) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM spool_batches WHERE task_id = ? AND acknowledged = 1",
                (str(task_id),),
            )
            connection.execute(
                "DELETE FROM task_checkpoints WHERE task_id = ?", (str(task_id),)
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = FULL;
                CREATE TABLE IF NOT EXISTS spool_batches (
                    batch_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    records_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                        CHECK (acknowledged IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS ix_spool_batches_pending
                    ON spool_batches(acknowledged, created_at, batch_id);
                CREATE TABLE IF NOT EXISTS task_checkpoints (
                    task_id TEXT PRIMARY KEY,
                    page INTEGER NOT NULL CHECK (page >= 0),
                    processed_count INTEGER NOT NULL CHECK (processed_count >= 0)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _batch_from_row(row: sqlite3.Row) -> SpoolBatch:
        records = [
            ParsedJobRecord.model_validate(item)
            for item in json.loads(str(row["records_json"]))
        ]
        created_at = datetime.fromisoformat(str(row["created_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return SpoolBatch(
            batch_id=UUID(str(row["batch_id"])),
            task_id=UUID(str(row["task_id"])),
            records=records,
            created_at=created_at,
        )
