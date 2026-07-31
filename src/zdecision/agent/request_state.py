"""Durable intent journal for native app-server mutations."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from zdecision.app_server.jsonl import AppServerRequestError
from zdecision.app_server.models import AppServerTurnReceipt
from zdecision.jsonio import canonical_json_bytes


NativeStage = Literal[
    "capture_fork",
    "inventory",
    "extraction",
    "reconciliation_thread",
    "reconciliation_turn",
]
NativeAttemptState = Literal["prepared", "pending", "attached", "completed"]

_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGES = frozenset(
    (
        "capture_fork",
        "inventory",
        "extraction",
        "reconciliation_thread",
        "reconciliation_turn",
    )
)


class RequestStateError(Exception):
    """Base class for native-attempt state failures."""


class NativeAttemptNotFound(RequestStateError):
    """A requested native attempt has not been prepared."""


class NativeAttemptConflict(RequestStateError):
    """A replay disagrees with the durably recorded native attempt."""


class CaptureResultUnknown(RequestStateError):
    """An external mutation may have succeeded but cannot yet be adopted."""


@dataclass(frozen=True)
class NativeAttempt:
    request_id: str
    operation_key: str
    stage: NativeStage
    stable_tag: str
    state: NativeAttemptState
    native_id: str | None
    output_digest: str | None


class RequestStateStore:
    """Persist app-server intent before any non-idempotent native call."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "RequestStateStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS native_attempts (
                    request_id TEXT NOT NULL,
                    operation_key TEXT NOT NULL,
                    stage TEXT NOT NULL CHECK(stage IN (
                        'capture_fork',
                        'inventory',
                        'extraction',
                        'reconciliation_thread',
                        'reconciliation_turn'
                    )),
                    stable_tag TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared',
                        'pending',
                        'attached',
                        'completed'
                    )),
                    native_id TEXT,
                    output_digest TEXT,
                    PRIMARY KEY(request_id, operation_key, stage)
                );
                """
            )
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def get_native_attempt(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
    ) -> NativeAttempt | None:
        identity = _identity(request_id, operation_key, stage)
        row = self._connection.execute(
            """
            SELECT request_id, operation_key, stage, stable_tag, state,
                   native_id, output_digest
            FROM native_attempts
            WHERE request_id = ? AND operation_key = ? AND stage = ?
            """,
            identity,
        ).fetchone()
        return None if row is None else _attempt(row)

    def get_or_create_native_attempt(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
        stable_tag: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        tag = _safe_value(stable_tag, "stable_tag")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._row(identity)
            if existing is not None:
                attempt = _attempt(existing)
                if attempt.stable_tag != tag:
                    raise NativeAttemptConflict(
                        "Native attempt stable tag conflicts"
                    )
                self._connection.commit()
                return attempt
            self._connection.execute(
                """
                INSERT INTO native_attempts(
                    request_id, operation_key, stage, stable_tag, state,
                    native_id, output_digest
                ) VALUES (?, ?, ?, ?, 'prepared', NULL, NULL)
                """,
                (*identity, tag),
            )
            attempt = _attempt(self._required_row(identity))
            self._connection.commit()
            return attempt
        except Exception:
            self._connection.rollback()
            raise

    def mark_native_pending(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        return self._transition(
            identity,
            allowed=frozenset(("prepared", "pending")),
            target="pending",
        )

    def attach_native_result(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
        native_id: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        result_id = _safe_value(native_id, "native_id")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = _attempt(self._required_row(identity))
            if current.state == "prepared":
                raise NativeAttemptConflict(
                    "Native result cannot attach before pending"
                )
            if current.native_id is not None:
                if current.native_id != result_id:
                    raise NativeAttemptConflict(
                        "Native attempt result conflicts"
                    )
                self._connection.commit()
                return current
            if current.state != "pending":
                raise NativeAttemptConflict("Native attempt state conflicts")
            self._connection.execute(
                """
                UPDATE native_attempts
                SET state = 'attached', native_id = ?
                WHERE request_id = ? AND operation_key = ? AND stage = ?
                """,
                (result_id, *identity),
            )
            attached = _attempt(self._required_row(identity))
            self._connection.commit()
            return attached
        except Exception:
            self._connection.rollback()
            raise

    def complete_native_attempt(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
        output_digest: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        digest = _digest(output_digest)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = _attempt(self._required_row(identity))
            if current.native_id is None or current.state not in {
                "attached",
                "completed",
            }:
                raise NativeAttemptConflict(
                    "Native attempt cannot complete before attachment"
                )
            if current.output_digest is not None:
                if current.output_digest != digest:
                    raise NativeAttemptConflict(
                        "Native attempt output digest conflicts"
                    )
                self._connection.commit()
                return current
            self._connection.execute(
                """
                UPDATE native_attempts
                SET state = 'completed', output_digest = ?
                WHERE request_id = ? AND operation_key = ? AND stage = ?
                """,
                (digest, *identity),
            )
            completed = _attempt(self._required_row(identity))
            self._connection.commit()
            return completed
        except Exception:
            self._connection.rollback()
            raise

    def reset_native_after_rejection(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = _attempt(self._required_row(identity))
            if current.state == "prepared":
                self._connection.commit()
                return current
            if current.state != "pending" or current.native_id is not None:
                raise NativeAttemptConflict(
                    "Attached native attempts cannot be reset"
                )
            self._connection.execute(
                """
                UPDATE native_attempts
                SET state = 'prepared', native_id = NULL, output_digest = NULL
                WHERE request_id = ? AND operation_key = ? AND stage = ?
                """,
                identity,
            )
            reset = _attempt(self._required_row(identity))
            self._connection.commit()
            return reset
        except Exception:
            self._connection.rollback()
            raise

    def _transition(
        self,
        identity: tuple[str, str, str],
        *,
        allowed: frozenset[str],
        target: NativeAttemptState,
    ) -> NativeAttempt:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = _attempt(self._required_row(identity))
            if current.state not in allowed:
                raise NativeAttemptConflict("Native attempt state conflicts")
            if current.state != target:
                self._connection.execute(
                    """
                    UPDATE native_attempts
                    SET state = ?
                    WHERE request_id = ? AND operation_key = ? AND stage = ?
                    """,
                    (target, *identity),
                )
            transitioned = _attempt(self._required_row(identity))
            self._connection.commit()
            return transitioned
        except Exception:
            self._connection.rollback()
            raise

    def _row(
        self, identity: tuple[str, str, str]
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT request_id, operation_key, stage, stable_tag, state,
                   native_id, output_digest
            FROM native_attempts
            WHERE request_id = ? AND operation_key = ? AND stage = ?
            """,
            identity,
        ).fetchone()

    def _required_row(
        self, identity: tuple[str, str, str]
    ) -> sqlite3.Row:
        row = self._row(identity)
        if row is None:
            raise NativeAttemptNotFound("Native attempt does not exist")
        return row


class NativeCallCoordinator:
    """Combine durable intent with unique native-object recovery."""

    def __init__(self, store: RequestStateStore) -> None:
        if not isinstance(store, RequestStateStore):
            raise TypeError("store must be a RequestStateStore")
        self.store = store

    def resolve_thread(
        self,
        *,
        request_id: str,
        operation_key: str,
        stage: str,
        stable_tag: str,
        find: Callable[[str], str | None],
        create: Callable[[], str],
    ) -> str:
        attempt = self.store.get_or_create_native_attempt(
            request_id, operation_key, stage, stable_tag
        )
        if attempt.state == "completed":
            assert attempt.native_id is not None
            return attempt.native_id
        if attempt.state == "attached":
            assert attempt.native_id is not None
            self.store.complete_native_attempt(
                request_id,
                operation_key,
                stage,
                _thread_digest(attempt.native_id, stage, stable_tag),
            )
            return attempt.native_id
        if attempt.state == "pending":
            native_id = self._find_thread(find, stable_tag)
            if native_id is None:
                raise CaptureResultUnknown(
                    "Native Thread result is not yet observable"
                )
            attached = self.store.attach_native_result(
                request_id, operation_key, stage, native_id
            )
            self.store.complete_native_attempt(
                request_id,
                operation_key,
                stage,
                _thread_digest(native_id, stage, stable_tag),
            )
            assert attached.native_id is not None
            return attached.native_id

        self.store.mark_native_pending(request_id, operation_key, stage)
        try:
            native_id = create()
            _safe_value(native_id, "native_id")
        except AppServerRequestError:
            self.store.reset_native_after_rejection(
                request_id, operation_key, stage
            )
            raise
        except Exception:
            raise CaptureResultUnknown(
                "Native Thread result is unknown"
            ) from None
        self.store.attach_native_result(
            request_id, operation_key, stage, native_id
        )
        self.store.complete_native_attempt(
            request_id,
            operation_key,
            stage,
            _thread_digest(native_id, stage, stable_tag),
        )
        return native_id

    def resolve_structured_turn(
        self,
        *,
        request_id: str,
        operation_key: str,
        stage: str,
        stable_tag: str,
        read: Callable[[str], AppServerTurnReceipt | None],
        create: Callable[[], AppServerTurnReceipt],
    ) -> AppServerTurnReceipt:
        attempt = self.store.get_or_create_native_attempt(
            request_id, operation_key, stage, stable_tag
        )
        if attempt.state in {"pending", "attached", "completed"}:
            receipt = self._read_turn(read, stable_tag)
            if receipt is None:
                raise CaptureResultUnknown(
                    "Native Turn result is not yet observable"
                )
            self._verify_receipt(attempt, receipt)
            if attempt.native_id is None:
                self.store.attach_native_result(
                    request_id,
                    operation_key,
                    stage,
                    receipt.turn_id,
                )
            self.store.complete_native_attempt(
                request_id,
                operation_key,
                stage,
                receipt.output_sha256,
            )
            return receipt

        self.store.mark_native_pending(request_id, operation_key, stage)
        try:
            receipt = create()
            if not isinstance(receipt, AppServerTurnReceipt):
                raise TypeError("create did not return an app-server receipt")
        except AppServerRequestError:
            self.store.reset_native_after_rejection(
                request_id, operation_key, stage
            )
            raise
        except Exception:
            raise CaptureResultUnknown(
                "Native Turn result is unknown"
            ) from None
        self.store.attach_native_result(
            request_id, operation_key, stage, receipt.turn_id
        )
        self.store.complete_native_attempt(
            request_id,
            operation_key,
            stage,
            receipt.output_sha256,
        )
        return receipt

    @staticmethod
    def _find_thread(
        find: Callable[[str], str | None], stable_tag: str
    ) -> str | None:
        try:
            native_id = find(stable_tag)
            if native_id is not None:
                return _safe_value(native_id, "native_id")
            return None
        except Exception:
            raise CaptureResultUnknown(
                "Native Thread recovery is unavailable"
            ) from None

    @staticmethod
    def _read_turn(
        read: Callable[[str], AppServerTurnReceipt | None],
        stable_tag: str,
    ) -> AppServerTurnReceipt | None:
        try:
            receipt = read(stable_tag)
            if receipt is not None and not isinstance(
                receipt, AppServerTurnReceipt
            ):
                raise TypeError("read did not return an app-server receipt")
            return receipt
        except Exception:
            raise CaptureResultUnknown(
                "Native Turn recovery is unavailable"
            ) from None

    @staticmethod
    def _verify_receipt(
        attempt: NativeAttempt, receipt: AppServerTurnReceipt
    ) -> None:
        if (
            attempt.native_id is not None
            and attempt.native_id != receipt.turn_id
        ):
            raise NativeAttemptConflict("Recovered native Turn id conflicts")
        if (
            attempt.output_digest is not None
            and attempt.output_digest != receipt.output_sha256
        ):
            raise NativeAttemptConflict(
                "Recovered native Turn output conflicts"
            )


def _identity(
    request_id: str, operation_key: str, stage: str
) -> tuple[str, str, str]:
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise ValueError("request_id is invalid")
    operation = _safe_value(operation_key, "operation_key")
    if not isinstance(stage, str) or stage not in _STAGES:
        raise ValueError("stage is invalid")
    return request_id, operation, stage


def _safe_value(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_KEY.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("output_digest is invalid")
    return value


def _thread_digest(native_id: str, stage: str, stable_tag: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "native_id": native_id,
                "stable_tag": stable_tag,
                "stage": stage,
            }
        )
    ).hexdigest()


def _attempt(row: sqlite3.Row) -> NativeAttempt:
    return NativeAttempt(
        request_id=row["request_id"],
        operation_key=row["operation_key"],
        stage=row["stage"],
        stable_tag=row["stable_tag"],
        state=row["state"],
        native_id=row["native_id"],
        output_digest=row["output_digest"],
    )
