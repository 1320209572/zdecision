"""SQLite persistence owned by the central coordination service."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from zdecision.central.auth import require_id
from zdecision.sync.contracts import RepositoryView


@dataclass(frozen=True)
class CaptureRequestRecord:
    request_id: str
    organization_id: str
    actor_id: str
    repository_id: str
    product_id: str
    product_name: str
    template_id: str
    state: str
    attempt_count: int
    claimed_device_id: str | None
    lease_token_digest: str | None
    lease_expires_at: str | None
    retry_at: str | None
    result_batch_digest: str | None
    terminal_code: str | None
    last_sequence: int
    created_at: str
    updated_at: str


class CentralStore:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> "CentralStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS repository_mappings (
                    organization_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    PRIMARY KEY(organization_id, repository_id)
                );

                CREATE TABLE IF NOT EXISTS capture_requests (
                    request_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'queued','claimed','running','succeeded',
                        'succeeded_no_candidates','failed_retryable',
                        'failed_terminal','cancelled'
                    )),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_device_id TEXT,
                    lease_token_digest TEXT,
                    lease_expires_at TEXT,
                    retry_at TEXT,
                    result_batch_digest TEXT,
                    terminal_code TEXT,
                    last_sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS capture_request_actions (
                    organization_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    client_action_id TEXT NOT NULL,
                    request_id TEXT NOT NULL
                        REFERENCES capture_requests(request_id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(
                        organization_id, actor_id, client_action_id
                    )
                );

                CREATE TABLE IF NOT EXISTS capture_request_events (
                    request_id TEXT NOT NULL
                        REFERENCES capture_requests(request_id),
                    sequence INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    code TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(request_id, sequence)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    one_active_capture_per_repository
                ON capture_requests(organization_id, repository_id)
                WHERE state IN (
                    'queued','claimed','running','failed_retryable'
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    capture_event_sequence_once
                ON capture_request_events(request_id, sequence);

                CREATE INDEX IF NOT EXISTS capture_requests_claim_order
                ON capture_requests(
                    organization_id, state, retry_at, created_at, request_id
                );
                """
            )
        return cls(database_path, connection)

    def close(self) -> None:
        self.connection.close()

    def put_repository_mapping(
        self,
        organization_id: str,
        repository: RepositoryView,
    ) -> None:
        organization = require_id(organization_id, "organization_id")
        if not isinstance(repository, RepositoryView):
            raise TypeError("repository must be a RepositoryView")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO repository_mappings(
                    organization_id, repository_id, product_id,
                    product_name, enabled
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(organization_id, repository_id) DO UPDATE SET
                    product_id = excluded.product_id,
                    product_name = excluded.product_name,
                    enabled = excluded.enabled
                """,
                (
                    organization,
                    repository.repository_id,
                    repository.product_id,
                    repository.product_name,
                    int(repository.enabled),
                ),
            )

    def get_request_record(
        self, request_id: str
    ) -> CaptureRequestRecord | None:
        row = self.connection.execute(
            "SELECT * FROM capture_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return request_record_from_row(row)


def request_record_from_row(row: sqlite3.Row) -> CaptureRequestRecord:
    return CaptureRequestRecord(
        request_id=row["request_id"],
        organization_id=row["organization_id"],
        actor_id=row["actor_id"],
        repository_id=row["repository_id"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        template_id=row["template_id"],
        state=row["state"],
        attempt_count=row["attempt_count"],
        claimed_device_id=row["claimed_device_id"],
        lease_token_digest=row["lease_token_digest"],
        lease_expires_at=row["lease_expires_at"],
        retry_at=row["retry_at"],
        result_batch_digest=row["result_batch_digest"],
        terminal_code=row["terminal_code"],
        last_sequence=row["last_sequence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
