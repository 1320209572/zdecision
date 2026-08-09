"""Test-only durable state owner for the Recall elicitation probe."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Literal, Sequence

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict


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

_CASE_CHOICES = ("accept", "decline", "cancel", "capability_unavailable", "restart")
_CASES = frozenset(_CASE_CHOICES)
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
_REQUEST_DIGEST_DOMAIN = b"zdecision-elicitation-e0-request-v1"

ELICITATION_MESSAGE = (
    "是否启用本任务的 ZDecision 正式决策召回？"
    "确认后仅对当前 Codex Session 生效。"
)


class EmptyConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


def supports_form_elicitation(context: Context) -> bool:
    params = context.session.client_params
    elicitation = None if params is None else params.capabilities.elicitation
    return bool(elicitation is not None and elicitation.form is not None)


def request_digest(request_id: object) -> str:
    digest = hashlib.sha256()
    digest.update(_REQUEST_DIGEST_DOMAIN)
    digest.update(b"\x00")
    digest.update(str(request_id).encode("utf-8"))
    return digest.hexdigest()


def build_probe_server(database_path: Path) -> FastMCP:
    server = FastMCP("ZDecision Recall E0 Probe")

    @server.tool(
        title="Probe ZDecision user confirmation",
        description="Run the test-only ZDecision E0 native confirmation probe.",
    )
    async def probe_zdecision_elicitation(
        context: Context,
    ) -> dict[str, object]:
        return await _run_probe(context=context, database_path=database_path)

    return server


async def _run_probe(*, context: Context, database_path: Path) -> dict[str, object]:
    store = ProbeReceiptStore.open(database_path)
    try:
        receipt = store.current()
        if receipt is None:
            return _bounded_result(
                state="unavailable",
                replayed=False,
                prompt_count=0,
                completion_count=0,
            )
        if receipt.state == "pending":
            return _result_for_receipt(receipt, replayed=True)
        if receipt.state != "armed":
            return _result_for_receipt(receipt, replayed=True)
        if not supports_form_elicitation(context):
            receipt = store.mark_armed_unavailable(now=datetime.now(UTC))
            return _result_for_receipt(receipt, replayed=False)

        pending = store.claim_armed(
            request_digest=request_digest(context.request_id),
            now=datetime.now(UTC),
        )
        try:
            result = await context.elicit(
                message=ELICITATION_MESSAGE,
                schema=EmptyConfirmation,
            )
            if result.action not in {"accept", "decline", "cancel"}:
                raise ValueError("invalid elicitation action")
            receipt = store.complete(
                pending.case_id,
                state=result.action,
                now=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(_complete_failed(store, pending.case_id))
            except Exception:
                pass
            raise
        except Exception:
            receipt = store.complete(
                pending.case_id,
                state="failed",
                now=datetime.now(UTC),
            )
        return _result_for_receipt(receipt, replayed=False)
    finally:
        store.close()


async def _complete_failed(
    store: ProbeReceiptStore, case_id: ProbeCase
) -> ProbeReceipt:
    return store.complete(case_id, state="failed", now=datetime.now(UTC))


def _result_for_receipt(
    receipt: ProbeReceipt, *, replayed: bool
) -> dict[str, object]:
    return _bounded_result(
        state=receipt.state,
        replayed=replayed,
        prompt_count=receipt.prompt_count,
        completion_count=receipt.completion_count,
    )


def _bounded_result(
    *,
    state: ProbeState,
    replayed: bool,
    prompt_count: int,
    completion_count: int,
) -> dict[str, object]:
    return {
        "gate": "E0",
        "action": state,
        "authorized": state == "accept",
        "replayed": replayed,
        "prompt_count": prompt_count,
        "completion_count": completion_count,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recall_elicitation_probe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    arm_parser = subparsers.add_parser("arm")
    arm_parser.add_argument("--database", type=Path, required=True)
    arm_parser.add_argument("--case", choices=_CASE_CHOICES, required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--database", type=Path, required=True)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--database", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "arm":
            store = ProbeReceiptStore.open(args.database)
            try:
                store.arm(args.case, now=datetime.now(UTC))
            finally:
                store.close()
            return 0

        if args.command == "report":
            store = ProbeReceiptStore.open(args.database)
            try:
                report = store.report()
            finally:
                store.close()
            json.dump(
                report,
                sys.stdout,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            sys.stdout.write("\n")
            return 0

        store = ProbeReceiptStore.open(args.database)
        try:
            store.recover_pending(now=datetime.now(UTC))
        finally:
            store.close()
        build_probe_server(args.database).run(transport="stdio")
        return 0
    except Exception:
        print("probe_error", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
