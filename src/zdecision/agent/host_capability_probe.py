"""Private state for the disposable Recall MCP Apps host-capability probe."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal


PROBE_VERSION = 1
PROBE_TTL = timedelta(hours=24)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")
_PROBE_ID_PATTERN = re.compile(r"^probe_[A-Za-z0-9_-]{32}$")
_CREATE_ATTEMPTS = 8


@dataclass(frozen=True)
class HostCapabilityProbe:
    probe_id: str
    probe_version: int
    state: Literal["ready", "committed", "failed", "expired"]
    marker: str
    receipt: str
    created_at: str
    committed_at: str | None
    expires_at: str


class HostCapabilityProbeStore:
    """SQLite owner for one bounded, replayable host capability operation."""

    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime],
        token: Callable[[], str],
    ) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = connection
        self._clock = clock
        self._token = token

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        token: Callable[[], str] | None = None,
    ) -> "HostCapabilityProbeStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(database_path.parent, 0o700)
        connection = sqlite3.connect(database_path, timeout=5.0)
        try:
            os.chmod(database_path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recall_host_capability_probes (
                        probe_id TEXT PRIMARY KEY,
                        probe_version INTEGER NOT NULL
                            CHECK(probe_version = 1),
                        state TEXT NOT NULL
                            CHECK(state IN (
                                'ready', 'committed', 'failed', 'expired'
                            )),
                        marker TEXT NOT NULL UNIQUE,
                        receipt TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        committed_at TEXT,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
        except BaseException:
            connection.close()
            raise
        return cls(
            database_path,
            connection,
            clock=clock or (lambda: datetime.now(UTC)),
            token=token or (lambda: secrets.token_urlsafe(24)),
        )

    def create(self) -> HostCapabilityProbe:
        connection = self._require_connection()
        now = self._now()
        created_at = _serialize_utc(now)
        expires_at = _serialize_utc(now + PROBE_TTL)
        for _ in range(_CREATE_ATTEMPTS):
            probe_id = f"probe_{self._next_token()}"
            marker = f"ZDECISION_HOST_PROBE_{self._next_token()}"
            receipt = f"receipt_{self._next_token()}"
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO recall_host_capability_probes (
                            probe_id, probe_version, state, marker, receipt,
                            created_at, committed_at, expires_at
                        ) VALUES (?, ?, 'ready', ?, ?, ?, NULL, ?)
                        """,
                        (
                            probe_id,
                            PROBE_VERSION,
                            marker,
                            receipt,
                            created_at,
                            expires_at,
                        ),
                    )
            except sqlite3.IntegrityError:
                continue
            record = self.get(probe_id)
            if record is None:  # pragma: no cover - protects SQLite corruption
                raise RuntimeError("created host probe is unavailable")
            return record
        raise RuntimeError("unable to allocate a unique host probe")

    def commit(self, probe_id: str) -> HostCapabilityProbe | None:
        if not _valid_probe_id(probe_id):
            return None
        connection = self._require_connection()
        now = self._now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _select(connection, probe_id)
            if row is None:
                connection.commit()
                return None
            record = _record(row)
            if record.state == "committed":
                connection.commit()
                return record
            if record.state != "ready" or now >= _parse_utc(record.expires_at):
                if record.state == "ready":
                    connection.execute(
                        """
                        UPDATE recall_host_capability_probes
                        SET state = 'expired'
                        WHERE probe_id = ? AND state = 'ready'
                        """,
                        (probe_id,),
                    )
                connection.commit()
                return None
            committed_at = _serialize_utc(now)
            connection.execute(
                """
                UPDATE recall_host_capability_probes
                SET state = 'committed', committed_at = ?
                WHERE probe_id = ? AND state = 'ready'
                """,
                (committed_at, probe_id),
            )
            committed = _select(connection, probe_id)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if committed is None:  # pragma: no cover - protects SQLite corruption
            raise RuntimeError("committed host probe is unavailable")
        return _record(committed)

    def get(self, probe_id: str) -> HostCapabilityProbe | None:
        if not _valid_probe_id(probe_id):
            return None
        connection = self._require_connection()
        now = self._now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _select(connection, probe_id)
            if row is None:
                connection.commit()
                return None
            record = _record(row)
            if record.state == "ready" and now >= _parse_utc(record.expires_at):
                connection.execute(
                    """
                    UPDATE recall_host_capability_probes
                    SET state = 'expired'
                    WHERE probe_id = ? AND state = 'ready'
                    """,
                    (probe_id,),
                )
                row = _select(connection, probe_id)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if row is None:  # pragma: no cover - narrowed above
            return None
        return _record(row)

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("host probe store is closed")
        return self._connection

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("host probe clock must return a UTC datetime")
        return value.astimezone(UTC)

    def _next_token(self) -> str:
        value = self._token()
        if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("host probe token is invalid")
        return value


def _valid_probe_id(probe_id: object) -> bool:
    return (
        isinstance(probe_id, str)
        and _PROBE_ID_PATTERN.fullmatch(probe_id) is not None
    )


def _serialize_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("host probe timestamp is invalid")
    return parsed.astimezone(UTC)


def _select(
    connection: sqlite3.Connection, probe_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT probe_id, probe_version, state, marker, receipt,
               created_at, committed_at, expires_at
        FROM recall_host_capability_probes
        WHERE probe_id = ?
        """,
        (probe_id,),
    ).fetchone()


def _record(row: sqlite3.Row) -> HostCapabilityProbe:
    return HostCapabilityProbe(
        probe_id=row["probe_id"],
        probe_version=row["probe_version"],
        state=row["state"],
        marker=row["marker"],
        receipt=row["receipt"],
        created_at=row["created_at"],
        committed_at=row["committed_at"],
        expires_at=row["expires_at"],
    )
