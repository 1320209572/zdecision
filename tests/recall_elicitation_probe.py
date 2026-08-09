"""Test-only durable state owner for the Recall elicitation probe."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Literal


ProbeCase = Literal[
    "accept", "decline", "cancel", "capability_unavailable", "restart"
]
ProbeState = Literal[
    "armed",
    "pending",
    "accept",
    "decline",
    "cancel",
    "unavailable",
    "failed",
    "transport_lost",
]

_CASES = frozenset(("accept", "decline", "cancel", "capability_unavailable", "restart"))
_STATES = frozenset(
    (
        "armed",
        "pending",
        "accept",
        "decline",
        "cancel",
        "unavailable",
        "failed",
        "transport_lost",
    )
)
_COMPLETION_STATES = frozenset(
    ("accept", "decline", "cancel", "unavailable", "failed", "transport_lost")
)
_CLIENT_ACTION_STATES = frozenset(("accept", "decline", "cancel"))
_REQUEST_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ProbeConflict(RuntimeError):
    """The requested probe transition is not valid for durable state."""


@dataclass(frozen=True)
class ProbeReceipt:
    case_id: ProbeCase
    state: ProbeState
    request_digest: str | None
    prompt_count: int
    completion_count: int
    updated_at: str


class ProbeReceiptStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> ProbeReceiptStore:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS elicitation_probe_receipts (
                case_id TEXT PRIMARY KEY CHECK(case_id IN (
                    'accept', 'decline', 'cancel', 'capability_unavailable', 'restart'
                )),
                state TEXT NOT NULL CHECK(state IN (
                    'armed', 'pending', 'accept', 'decline', 'cancel',
                    'unavailable', 'failed', 'transport_lost'
                )),
                is_current INTEGER NOT NULL CHECK(is_current IN (0, 1)),
                request_digest TEXT,
                prompt_count INTEGER NOT NULL CHECK(prompt_count BETWEEN 0 AND 1),
                completion_count INTEGER NOT NULL CHECK(completion_count BETWEEN 0 AND 1),
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_current_elicitation_probe
            ON elicitation_probe_receipts(is_current)
            WHERE is_current = 1
            """
        )
        connection.commit()
        return cls(connection)

    def arm(self, case_id: ProbeCase, *, now: datetime) -> ProbeReceipt:
        _validate_case(case_id)
        timestamp = _timestamp(now)
        with self._transaction():
            if self._row_for_case(case_id) is not None:
                raise ProbeConflict("probe case already exists")
            if self._connection.execute(
                "SELECT 1 FROM elicitation_probe_receipts WHERE state IN ('armed', 'pending')"
            ).fetchone() is not None:
                raise ProbeConflict("another probe case is active")
            self._connection.execute(
                "UPDATE elicitation_probe_receipts SET is_current = 0 WHERE is_current = 1"
            )
            self._connection.execute(
                """
                INSERT INTO elicitation_probe_receipts (
                    case_id, state, is_current, request_digest, prompt_count,
                    completion_count, updated_at
                ) VALUES (?, 'armed', 1, NULL, 0, 0, ?)
                """,
                (case_id, timestamp),
            )
            return self._require_case(case_id)

    def claim_armed(self, *, request_digest: str, now: datetime) -> ProbeReceipt:
        _validate_digest(request_digest)
        timestamp = _timestamp(now)
        with self._transaction():
            current = self._require_current()
            if current["state"] != "armed":
                raise ProbeConflict("current probe case is not armed")
            self._connection.execute(
                """
                UPDATE elicitation_probe_receipts
                SET state = 'pending', request_digest = ?, prompt_count = 1, updated_at = ?
                WHERE case_id = ?
                """,
                (request_digest, timestamp, current["case_id"]),
            )
            return self._require_case(current["case_id"])

    def mark_armed_unavailable(self, *, now: datetime) -> ProbeReceipt:
        timestamp = _timestamp(now)
        with self._transaction():
            current = self._require_current()
            if current["state"] != "armed":
                raise ProbeConflict("current probe case is not armed")
            self._connection.execute(
                """
                UPDATE elicitation_probe_receipts
                SET state = 'unavailable', updated_at = ?
                WHERE case_id = ?
                """,
                (timestamp, current["case_id"]),
            )
            return self._require_case(current["case_id"])

    def complete(
        self, case_id: ProbeCase, *, state: ProbeState, now: datetime
    ) -> ProbeReceipt:
        _validate_case(case_id)
        _validate_state(state)
        if state not in _COMPLETION_STATES:
            raise ValueError("state is not a completion state")
        timestamp = _timestamp(now)
        with self._transaction():
            current = self._require_current()
            if current["state"] != "pending" or current["case_id"] != case_id:
                raise ProbeConflict("probe case is not the current pending case")
            completion_count = 1 if state in _CLIENT_ACTION_STATES else 0
            self._connection.execute(
                """
                UPDATE elicitation_probe_receipts
                SET state = ?, completion_count = ?, updated_at = ?
                WHERE case_id = ?
                """,
                (state, completion_count, timestamp, case_id),
            )
            return self._require_case(case_id)

    def recover_pending(self, *, now: datetime) -> tuple[ProbeReceipt, ...]:
        timestamp = _timestamp(now)
        with self._transaction():
            rows = tuple(
                self._connection.execute(
                    "SELECT * FROM elicitation_probe_receipts WHERE state = 'pending' ORDER BY case_id"
                )
            )
            self._connection.execute(
                """
                UPDATE elicitation_probe_receipts
                SET state = 'transport_lost', completion_count = 0, updated_at = ?
                WHERE state = 'pending'
                """,
                (timestamp,),
            )
            return tuple(self._require_case(row["case_id"]) for row in rows)

    def current(self) -> ProbeReceipt | None:
        row = self._connection.execute(
            "SELECT * FROM elicitation_probe_receipts WHERE is_current = 1"
        ).fetchone()
        return _receipt(row) if row is not None else None

    def receipt(self, case_id: ProbeCase) -> ProbeReceipt | None:
        _validate_case(case_id)
        row = self._row_for_case(case_id)
        return _receipt(row) if row is not None else None

    def receipts(self) -> tuple[ProbeReceipt, ...]:
        rows = self._connection.execute(
            "SELECT * FROM elicitation_probe_receipts ORDER BY case_id"
        )
        return tuple(_receipt(row) for row in rows)

    def report(self) -> dict[str, object]:
        receipts = self.receipts()
        return {
            "gate": "E0",
            "schema_version": 1,
            "cases": [
                {
                    "case_id": receipt.case_id,
                    "state": receipt.state,
                    "request_digest": receipt.request_digest,
                    "prompt_count": receipt.prompt_count,
                    "completion_count": receipt.completion_count,
                    "updated_at": receipt.updated_at,
                }
                for receipt in receipts
            ],
        }

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _row_for_case(self, case_id: ProbeCase) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM elicitation_probe_receipts WHERE case_id = ?", (case_id,)
        ).fetchone()

    def _require_case(self, case_id: ProbeCase) -> ProbeReceipt:
        row = self._row_for_case(case_id)
        if row is None:
            raise ProbeConflict("probe case is absent")
        return _receipt(row)

    def _require_current(self) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM elicitation_probe_receipts WHERE is_current = 1"
        ).fetchone()
        if row is None:
            raise ProbeConflict("no current probe case")
        return row


def _receipt(row: sqlite3.Row) -> ProbeReceipt:
    return ProbeReceipt(
        case_id=row["case_id"],
        state=row["state"],
        request_digest=row["request_digest"],
        prompt_count=row["prompt_count"],
        completion_count=row["completion_count"],
        updated_at=row["updated_at"],
    )


def _validate_case(case_id: object) -> None:
    if case_id not in _CASES:
        raise ValueError("invalid probe case")


def _validate_state(state: object) -> None:
    if state not in _STATES:
        raise ValueError("invalid probe state")


def _validate_digest(request_digest: object) -> None:
    if not isinstance(request_digest, str) or _REQUEST_DIGEST.fullmatch(request_digest) is None:
        raise ValueError("request_digest must be 64 lowercase hexadecimal characters")


def _timestamp(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
