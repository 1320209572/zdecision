"""Disposable, test-only Recall Gate A0 Plugin, Hook, MCP server, and state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict


PROTOCOL_VERSION = "recall-handoff-v1"
APP_PROTOCOL_VERSION = "2026-01-26"
APPLICATION_INSTRUCTION = (
    "Classify every delivered Decision exactly once, then call "
    "apply_zdecision_gate_a0_delivery before development mutation."
)
RESOURCE_URI = "ui://zdecision/recall-gate-a0-v1.html"
RESOURCE_MIME = "text/html;profile=mcp-app"
RENDER_TOOL = "mcp__zdecision_gate_a0__show_zdecision_gate_a0"
APPLICATION_TOOL = "mcp__zdecision_gate_a0__apply_zdecision_gate_a0_delivery"
MUTATION_TOOL = "mcp__zdecision_gate_a0__increment_zdecision_gate_a0_counter"
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,255}$")
_OPAQUE = re.compile(
    r"^(?:attempt|delivery|application|mutation|delivery_receipt|"
    r"application_receipt)_[0-9a-f]{32}$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CATEGORIES = frozenset(("applicable", "not_applicable", "conflicting", "uncertain"))
_MARKER_NAME = ".zdecision-gate-a0-disposable.json"

FIXTURE_ONE_BYTES = (
    '{"claim":"Gate A0 fixture one requires server-authoritative handoff state.",'
    '"decision_id":"dec_11111111111111111111111111111111",'
    '"format":"zdecision-decision/v1",'
    '"future_action":"Keep the disposable delivery stable across remounts.",'
    '"invalidation_conditions":["The Gate A0 protocol version changes."],'
    '"lifecycle":"active",'
    '"product_id":"prod_4d7b16e1616dd4cd1aeb2411836fd687",'
    '"product_name":"安恒",'
    '"publication_preview_id":"pub_11111111111111111111111111111111",'
    '"review_approval":{"actor":"user",'
    '"recorded_at":"2026-08-10T00:00:00Z",'
    '"thread_id":"fixture-review-one",'
    '"turn_id":"fixture-review-turn-one"},'
    '"revision":1,"schema_version":1,'
    '"scope":{"paths":["tests/integration/"],'
    '"repositories":["https://example.invalid/zdecision-gate-a0.git"],'
    '"summary":"Disposable Gate A0 delivery behavior"},'
    '"source":{"thread_id":"fixture-source-one",'
    '"turn_id":"fixture-turn-one"},'
    '"supersedes":[],"variant_of":[]}\n'
)
FIXTURE_TWO_BYTES = (
    '{"claim":"Gate A0 fixture two limits application to validated classifications.",'
    '"decision_id":"dec_22222222222222222222222222222222",'
    '"format":"zdecision-decision/v1",'
    '"future_action":"Deny disposable mutation until application commits atomically.",'
    '"invalidation_conditions":["The Gate A0 application contract changes."],'
    '"lifecycle":"active",'
    '"product_id":"prod_4d7b16e1616dd4cd1aeb2411836fd687",'
    '"product_name":"安恒",'
    '"publication_preview_id":"pub_22222222222222222222222222222222",'
    '"review_approval":{"actor":"user",'
    '"recorded_at":"2026-08-10T00:01:00Z",'
    '"thread_id":"fixture-review-two",'
    '"turn_id":"fixture-review-turn-two"},'
    '"revision":1,"schema_version":1,'
    '"scope":{"paths":["tests/integration/"],'
    '"repositories":["https://example.invalid/zdecision-gate-a0.git"],'
    '"summary":"Disposable Gate A0 application guard"},'
    '"source":{"thread_id":"fixture-source-two",'
    '"turn_id":"fixture-turn-two"},'
    '"supersedes":[],"variant_of":[]}\n'
)
FIXTURES = (
    (
        json.loads(FIXTURE_ONE_BYTES),
        "30dc189935dd11c1f9e87a900235dcc693479cbdf69106d139c2320d194ab63a",
    ),
    (
        json.loads(FIXTURE_TWO_BYTES),
        "4dfba3631e4a669ac024759525c868125111142aaeafb238fcad57e2af16c99a",
    ),
)


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_bytes(_canonical_bytes(value))
    os.replace(temporary, path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(value, "utf-8")
    os.replace(temporary, path)


def _root(value: str, *, require_marker: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError("Gate A0 root must be a bounded absolute path")
    normalized = Path(os.path.normpath(path))
    if require_marker and not (normalized / _MARKER_NAME).is_file():
        raise ValueError("Gate A0 root marker is missing")
    return normalized


def _configuration(root: Path) -> dict[str, object]:
    value = json.loads((root / _MARKER_NAME).read_text("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("protocol_version") != PROTOCOL_VERSION
        or value.get("root") != str(root)
        or not isinstance(value.get("repository"), str)
        or not Path(value["repository"]).is_absolute()
    ):
        raise ValueError("Gate A0 root marker is invalid")
    return value


def _database_path(root: Path) -> Path:
    return root / "state/gate-a0.sqlite3"


class GateA0Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.configuration = _configuration(root)
        path = _database_path(root)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._initialize()
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)

    def _initialize(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    render_turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'pending_confirmation', 'context_prepared',
                        'host_delivered', 'application_committed'
                    )),
                    delivery_id TEXT UNIQUE,
                    UNIQUE(session_id, render_turn_id, cwd)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
                    snapshot_json TEXT NOT NULL,
                    receipt TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN (
                        'context_prepared', 'host_delivered',
                        'application_committed'
                    )),
                    context_update_count INTEGER NOT NULL DEFAULT 0
                        CHECK(context_update_count IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS application_bindings (
                    application_binding_id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL UNIQUE REFERENCES deliveries(delivery_id),
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    classifications_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS applications (
                    delivery_id TEXT PRIMARY KEY REFERENCES deliveries(delivery_id),
                    application_binding_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    receipt TEXT NOT NULL UNIQUE,
                    classifications_json TEXT NOT NULL,
                    active_fixture_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mutation_claims (
                    mutation_id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL UNIQUE REFERENCES applications(delivery_id),
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS mutation_counter (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    value INTEGER NOT NULL CHECK(value IN (0, 1))
                );
                INSERT OR IGNORE INTO mutation_counter(singleton, value) VALUES (1, 0);
                """
            )

    def close(self) -> None:
        self.connection.close()

    def _trusted_host(self, value: Mapping[str, object]) -> tuple[str, str, str]:
        if value.get("hook_event_name") != "PreToolUse" or "agent_id" in value:
            raise ValueError("invalid Hook envelope")
        session_id = value.get("session_id")
        turn_id = value.get("turn_id")
        cwd = value.get("cwd")
        if (
            not isinstance(session_id, str)
            or _ID.fullmatch(session_id) is None
            or not isinstance(turn_id, str)
            or _ID.fullmatch(turn_id) is None
            or not isinstance(cwd, str)
            or not Path(cwd).is_absolute()
        ):
            raise ValueError("invalid host coordinates")
        normalized = str(Path(cwd).resolve())
        repository = Path(str(self.configuration["repository"])).resolve()
        try:
            Path(normalized).relative_to(repository)
        except ValueError as error:
            raise ValueError("repository is not enabled") from error
        return session_id, turn_id, normalized

    def bind_render(self, value: Mapping[str, object]) -> str:
        session_id, turn_id, cwd = self._trusted_host(value)
        existing = self.connection.execute(
            """
            SELECT attempt_id FROM attempts
            WHERE session_id = ? AND render_turn_id = ? AND cwd = ?
            """,
            (session_id, turn_id, cwd),
        ).fetchone()
        if existing is not None:
            return existing["attempt_id"]
        attempt_id = f"attempt_{secrets.token_hex(16)}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, session_id, render_turn_id, cwd, state, delivery_id
                ) VALUES (?, ?, ?, ?, 'pending_confirmation', NULL)
                """,
                (attempt_id, session_id, turn_id, cwd),
            )
        return attempt_id

    def bind_application(
        self, value: Mapping[str, object]
    ) -> tuple[str, str, list[dict[str, object]]]:
        session_id, turn_id, cwd = self._trusted_host(value)
        classifications = _classifications(
            value.get("tool_input", {}).get("classifications")
            if isinstance(value.get("tool_input"), Mapping)
            else None
        )
        row = self.connection.execute(
            """
            SELECT d.delivery_id
            FROM deliveries d JOIN attempts a ON a.attempt_id = d.attempt_id
            WHERE a.session_id = ? AND a.cwd = ? AND d.state = 'host_delivered'
            ORDER BY d.rowid DESC LIMIT 1
            """,
            (session_id, cwd),
        ).fetchone()
        if row is None:
            raise ValueError("no delivered binding")
        delivery_id = row["delivery_id"]
        serialized = _canonical_bytes(classifications).decode("utf-8")
        existing = self.connection.execute(
            """
            SELECT application_binding_id, session_id, turn_id, cwd,
                   classifications_json
            FROM application_bindings WHERE delivery_id = ?
            """,
            (delivery_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["session_id"],
                existing["turn_id"],
                existing["cwd"],
                existing["classifications_json"],
            ) != (session_id, turn_id, cwd, serialized):
                raise ValueError("application binding changed")
            return existing["application_binding_id"], delivery_id, classifications
        binding_id = f"application_{secrets.token_hex(16)}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO application_bindings(
                    application_binding_id, delivery_id, session_id, turn_id,
                    cwd, classifications_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (binding_id, delivery_id, session_id, turn_id, cwd, serialized),
            )
        return binding_id, delivery_id, classifications

    def bind_mutation(self, value: Mapping[str, object]) -> str:
        session_id, turn_id, cwd = self._trusted_host(value)
        application = self.connection.execute(
            """
            SELECT delivery_id, classifications_json
            FROM applications
            WHERE session_id = ? AND cwd = ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (session_id, cwd),
        ).fetchone()
        if application is None:
            raise ValueError("application receipt is missing")
        categories = [
            item["classification"]
            for item in json.loads(application["classifications_json"])
        ]
        if categories != ["applicable", "not_applicable"]:
            raise ValueError("application does not permit mutation")
        counter = self.connection.execute(
            "SELECT value FROM mutation_counter WHERE singleton = 1"
        ).fetchone()["value"]
        if counter != 0:
            raise ValueError("mutation already completed")
        existing = self.connection.execute(
            """
            SELECT mutation_id, session_id, turn_id, cwd, used
            FROM mutation_claims WHERE delivery_id = ?
            """,
            (application["delivery_id"],),
        ).fetchone()
        if existing is not None:
            if existing["used"] != 0:
                raise ValueError("mutation already completed")
            if (existing["session_id"], existing["turn_id"], existing["cwd"]) != (
                session_id,
                turn_id,
                cwd,
            ):
                raise ValueError("mutation binding changed")
            return existing["mutation_id"]
        mutation_id = f"mutation_{secrets.token_hex(16)}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO mutation_claims(
                    mutation_id, delivery_id, session_id, turn_id, cwd, used
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    mutation_id,
                    application["delivery_id"],
                    session_id,
                    turn_id,
                    cwd,
                ),
            )
        return mutation_id

    def render(self, attempt_id: str) -> sqlite3.Row | None:
        if not _valid_opaque(attempt_id, "attempt"):
            return None
        return self.connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()

    def enable(self, attempt_id: str) -> tuple[sqlite3.Row, dict[str, object]] | None:
        if not _valid_opaque(attempt_id, "attempt"):
            return None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            attempt = self.connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                self.connection.commit()
                return None
            if attempt["delivery_id"] is not None:
                delivery = self.connection.execute(
                    "SELECT * FROM deliveries WHERE delivery_id = ?",
                    (attempt["delivery_id"],),
                ).fetchone()
                self.connection.commit()
                return delivery, json.loads(delivery["snapshot_json"])
            if attempt["state"] != "pending_confirmation":
                self.connection.commit()
                return None
            delivery_id = f"delivery_{secrets.token_hex(16)}"
            receipt = f"delivery_receipt_{secrets.token_hex(16)}"
            snapshot = _snapshot(delivery_id)
            snapshot_json = _canonical_bytes(snapshot).decode("utf-8")
            self.connection.execute(
                """
                INSERT INTO deliveries(
                    delivery_id, attempt_id, snapshot_json, receipt, state,
                    context_update_count
                ) VALUES (?, ?, ?, ?, 'context_prepared', 0)
                """,
                (delivery_id, attempt_id, snapshot_json, receipt),
            )
            self.connection.execute(
                """
                UPDATE attempts
                SET state = 'context_prepared', delivery_id = ?
                WHERE attempt_id = ?
                """,
                (delivery_id, attempt_id),
            )
            delivery = self.connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            self.connection.commit()
            return delivery, snapshot
        except BaseException:
            self.connection.rollback()
            raise

    def acknowledge(self, delivery_id: str) -> sqlite3.Row | None:
        if not _valid_opaque(delivery_id, "delivery"):
            return None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            delivery = self.connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if delivery is None:
                self.connection.commit()
                return None
            if delivery["state"] == "context_prepared":
                self.connection.execute(
                    """
                    UPDATE deliveries
                    SET state = 'host_delivered', context_update_count = 1
                    WHERE delivery_id = ?
                    """,
                    (delivery_id,),
                )
                self.connection.execute(
                    """
                    UPDATE attempts SET state = 'host_delivered'
                    WHERE attempt_id = ?
                    """,
                    (delivery["attempt_id"],),
                )
            delivery = self.connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            self.connection.commit()
            return delivery
        except BaseException:
            self.connection.rollback()
            raise

    def status(self, attempt_id: str) -> tuple[sqlite3.Row, sqlite3.Row | None] | None:
        attempt = self.render(attempt_id)
        if attempt is None:
            return None
        delivery = None
        if attempt["delivery_id"] is not None:
            delivery = self.connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?",
                (attempt["delivery_id"],),
            ).fetchone()
        return attempt, delivery

    def apply(
        self,
        application_binding_id: str,
        delivery_id: str,
        classifications: list[dict[str, object]],
    ) -> sqlite3.Row | None:
        if not _valid_opaque(application_binding_id, "application") or not _valid_opaque(
            delivery_id, "delivery"
        ):
            return None
        normalized = _classifications(classifications)
        serialized = _canonical_bytes(normalized).decode("utf-8")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT * FROM applications WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if existing is not None:
                self.connection.commit()
                if (
                    existing["application_binding_id"] == application_binding_id
                    and existing["classifications_json"] == serialized
                ):
                    return existing
                return None
            binding = self.connection.execute(
                """
                SELECT * FROM application_bindings
                WHERE application_binding_id = ? AND delivery_id = ?
                """,
                (application_binding_id, delivery_id),
            ).fetchone()
            delivery = self.connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if (
                binding is None
                or delivery is None
                or binding["classifications_json"] != serialized
                or delivery["state"] != "host_delivered"
            ):
                self.connection.commit()
                return None
            receipt = f"application_receipt_{secrets.token_hex(16)}"
            active_count = sum(
                item["classification"] == "applicable" for item in normalized
            )
            self.connection.execute(
                """
                INSERT INTO applications(
                    delivery_id, application_binding_id, session_id, turn_id,
                    cwd, receipt, classifications_json, active_fixture_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    application_binding_id,
                    binding["session_id"],
                    binding["turn_id"],
                    binding["cwd"],
                    receipt,
                    serialized,
                    active_count,
                ),
            )
            self.connection.execute(
                "UPDATE deliveries SET state = 'application_committed' WHERE delivery_id = ?",
                (delivery_id,),
            )
            self.connection.execute(
                """
                UPDATE attempts SET state = 'application_committed'
                WHERE attempt_id = ?
                """,
                (delivery["attempt_id"],),
            )
            application = self.connection.execute(
                "SELECT * FROM applications WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            self.connection.commit()
            return application
        except BaseException:
            self.connection.rollback()
            raise

    def increment(self, mutation_id: str) -> int | None:
        if not _valid_opaque(mutation_id, "mutation"):
            return None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            claim = self.connection.execute(
                "SELECT * FROM mutation_claims WHERE mutation_id = ?", (mutation_id,)
            ).fetchone()
            if claim is None:
                self.connection.commit()
                return None
            counter = self.connection.execute(
                "SELECT value FROM mutation_counter WHERE singleton = 1"
            ).fetchone()["value"]
            if claim["used"] == 1:
                self.connection.commit()
                return counter
            if counter != 0:
                self.connection.commit()
                return None
            self.connection.execute(
                "UPDATE mutation_counter SET value = 1 WHERE singleton = 1"
            )
            self.connection.execute(
                "UPDATE mutation_claims SET used = 1 WHERE mutation_id = ?",
                (mutation_id,),
            )
            self.connection.commit()
            return 1
        except BaseException:
            self.connection.rollback()
            raise

    def inspect(self) -> dict[str, object]:
        scalar = lambda sql: self.connection.execute(sql).fetchone()[0]
        delivery = self.connection.execute(
            "SELECT delivery_id, receipt FROM deliveries ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        application = self.connection.execute(
            "SELECT receipt FROM applications ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "attempt_count": scalar("SELECT COUNT(*) FROM attempts"),
            "delivery_count": scalar("SELECT COUNT(*) FROM deliveries"),
            "context_update_count": scalar(
                "SELECT COALESCE(SUM(context_update_count), 0) FROM deliveries"
            ),
            "application_count": scalar("SELECT COUNT(*) FROM applications"),
            "active_fixture_count": scalar(
                "SELECT COALESCE(SUM(active_fixture_count), 0) FROM applications"
            ),
            "mutation_count": scalar(
                "SELECT value FROM mutation_counter WHERE singleton = 1"
            ),
            "ui_message_count": 0,
            "app_server_start_count": 0,
            "delivery_id_prefix": (
                delivery["delivery_id"][:17] if delivery is not None else None
            ),
            "delivery_receipt_prefix": (
                delivery["receipt"][:20] if delivery is not None else None
            ),
            "application_receipt_prefix": (
                application["receipt"][:23] if application is not None else None
            ),
        }


def _valid_opaque(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix + "_")
        and _OPAQUE.fullmatch(value) is not None
    )


def _snapshot(delivery_id: str) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "delivery_id": delivery_id,
        "application_instruction": APPLICATION_INSTRUCTION,
        "decisions": [
            {
                "decision_id": fixture["decision_id"],
                "revision": fixture["revision"],
                "digest": digest,
                "decision_revision": fixture,
            }
            for fixture, digest in FIXTURES
        ],
    }


def _classifications(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(FIXTURES):
        raise ValueError("complete classifications are required")
    normalized: list[dict[str, object]] = []
    for supplied, (fixture, expected_digest) in zip(value, FIXTURES, strict=True):
        if not isinstance(supplied, Mapping):
            raise ValueError("classification must be an object")
        decision_id = supplied.get("decision_id")
        revision = supplied.get("revision")
        digest = supplied.get("digest")
        category = supplied.get("classification")
        reason = supplied.get("reason")
        if (
            decision_id != fixture["decision_id"]
            or revision != 1
            or isinstance(revision, bool)
            or digest != expected_digest
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or category not in _CATEGORIES
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason.encode("utf-8")) > 240
        ):
            raise ValueError("classification does not match the delivery")
        normalized.append(
            {
                "decision_id": decision_id,
                "revision": revision,
                "digest": digest,
                "classification": category,
                "reason": reason,
            }
        )
    return normalized


def _hook_response(
    decision: str,
    *,
    updated_input: Mapping[str, object] | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    output: dict[str, object] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }
    if updated_input is not None:
        output["updatedInput"] = dict(updated_input)
    if decision == "deny":
        output["permissionDecisionReason"] = reason or (
            "ZDecision Gate A0 denied this action until its exact application "
            "receipt is committed."
        )
    return {"hookSpecificOutput": output}


def run_hook(root: Path) -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, Mapping):
            raise ValueError("Hook input must be an object")
        store = GateA0Store(root)
        try:
            tool_name = value.get("tool_name")
            if tool_name == RENDER_TOOL:
                attempt_id = store.bind_render(value)
                return _hook_response("allow", updated_input={"attempt_id": attempt_id})
            if tool_name == APPLICATION_TOOL:
                binding_id, delivery_id, classifications = store.bind_application(value)
                return _hook_response(
                    "allow",
                    updated_input={
                        "application_binding_id": binding_id,
                        "delivery_id": delivery_id,
                        "classifications": classifications,
                    },
                )
            if tool_name == MUTATION_TOOL:
                mutation_id = store.bind_mutation(value)
                return _hook_response(
                    "allow", updated_input={"mutation_id": mutation_id}
                )
            return {}
        finally:
            store.close()
    except Exception:
        return _hook_response(
            "deny",
            reason=(
                "ZDecision Gate A0 could not establish the trusted binding; "
                "do not retry with guessed identifiers."
            ),
        )


def _tool_result(
    text: str,
    structured: dict[str, object],
    *,
    meta: dict[str, object] | None = None,
    error: bool = False,
) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=structured,
        _meta=meta or {},
        isError=error,
    )


def _failed(code: str) -> CallToolResult:
    return _tool_result(
        "ZDecision Gate A0 rejected the bounded request.",
        {"protocol_version": PROTOCOL_VERSION, "state": "failed", "code": code},
        error=True,
    )


def create_server(store: GateA0Store) -> FastMCP:
    server = FastMCP("zdecision-gate-a0")
    render_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    action_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    read_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.resource(
        RESOURCE_URI,
        name="zdecision-recall-gate-a0",
        title="ZDecision Gate A0",
        description="Disposable next-native-message Decision handoff card.",
        mime_type=RESOURCE_MIME,
        meta={
            "ui": {
                "prefersBorder": True,
                "csp": {"connectDomains": [], "resourceDomains": []},
            },
            "openai/widgetDescription": "Disposable ZDecision Gate A0 card.",
        },
    )
    def gate_resource() -> str:
        return (store.root / "plugin/static/recall-gate-a0-v1.html").read_text(
            "utf-8"
        )

    @server.tool(
        title="Show ZDecision Gate A0",
        description="Render the disposable Decision handoff confirmation card.",
        annotations=render_annotations,
        meta={
            "ui": {"resourceUri": RESOURCE_URI, "visibility": ["model", "app"]},
            "openai/outputTemplate": RESOURCE_URI,
        },
    )
    def show_zdecision_gate_a0(attempt_id: str) -> CallToolResult:
        attempt = store.render(attempt_id)
        if attempt is None:
            return _failed("invalid_attempt")
        return _tool_result(
            "ZDecision Gate A0 confirmation is ready.",
            {"protocol_version": PROTOCOL_VERSION, "state": attempt["state"]},
            meta={"zdecision/attempt_id": attempt_id},
        )

    @server.tool(
        title="Enable ZDecision Gate A0 delivery",
        description="Commit one disposable delivery from the trusted App action.",
        annotations=action_annotations,
        meta={"ui": {"visibility": ["app"]}},
    )
    def enable_zdecision_gate_a0_delivery(attempt_id: str) -> CallToolResult:
        enabled = store.enable(attempt_id)
        if enabled is None:
            return _failed("invalid_attempt")
        delivery, snapshot = enabled
        return _tool_result(
            "ZDecision Gate A0 delivery is stable and ready for host context.",
            {
                "protocol_version": PROTOCOL_VERSION,
                "state": delivery["state"],
                "snapshot": snapshot,
                "receipt": delivery["receipt"],
            },
            meta={"zdecision/attempt_id": attempt_id},
        )

    @server.tool(
        title="Acknowledge ZDecision Gate A0 delivery",
        description="Acknowledge one successful host context update.",
        annotations=action_annotations,
        meta={"ui": {"visibility": ["app"]}},
    )
    def ack_zdecision_gate_a0_delivery(delivery_id: str) -> CallToolResult:
        delivery = store.acknowledge(delivery_id)
        if delivery is None:
            return _failed("invalid_delivery")
        return _tool_result(
            "ZDecision Gate A0 host delivery is acknowledged.",
            {
                "protocol_version": PROTOCOL_VERSION,
                "state": delivery["state"],
                "receipt": delivery["receipt"],
            },
            meta={"zdecision/attempt_id": delivery["attempt_id"]},
        )

    @server.tool(
        title="Get ZDecision Gate A0 status",
        description="Recover the authoritative disposable handoff state.",
        annotations=read_annotations,
        meta={"ui": {"visibility": ["app"]}},
    )
    def get_zdecision_gate_a0_status(attempt_id: str) -> CallToolResult:
        status = store.status(attempt_id)
        if status is None:
            return _failed("invalid_attempt")
        attempt, delivery = status
        structured: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "state": attempt["state"],
        }
        if delivery is not None:
            structured["receipt"] = delivery["receipt"]
        return _tool_result(
            "ZDecision Gate A0 authoritative state was recovered.",
            structured,
            meta={"zdecision/attempt_id": attempt_id},
        )

    @server.tool(
        title="Apply ZDecision Gate A0 delivery",
        description="Commit complete four-category classifications for one delivery.",
        annotations=action_annotations,
        meta={"ui": {"visibility": ["model", "app"]}},
    )
    def apply_zdecision_gate_a0_delivery(
        application_binding_id: str,
        delivery_id: str,
        classifications: list[dict[str, object]],
    ) -> CallToolResult:
        application = store.apply(
            application_binding_id, delivery_id, classifications
        )
        if application is None:
            return _failed("invalid_application")
        return _tool_result(
            "ZDecision Gate A0 application is committed.",
            {
                "protocol_version": PROTOCOL_VERSION,
                "state": "application_committed",
                "application_receipt": application["receipt"],
                "active_fixture_count": application["active_fixture_count"],
            },
        )

    @server.tool(
        title="Increment ZDecision Gate A0 counter",
        description="Increment only the disposable SQLite mutation counter.",
        annotations=action_annotations,
        meta={"ui": {"visibility": ["model", "app"]}},
    )
    def increment_zdecision_gate_a0_counter(mutation_id: str) -> CallToolResult:
        counter = store.increment(mutation_id)
        if counter is None:
            return _failed("mutation_denied")
        return _tool_result(
            "ZDecision Gate A0 disposable counter incremented.",
            {
                "protocol_version": PROTOCOL_VERSION,
                "state": "mutated",
                "counter": counter,
            },
        )

    for tool_name in (
        "show_zdecision_gate_a0",
        "enable_zdecision_gate_a0_delivery",
        "ack_zdecision_gate_a0_delivery",
        "get_zdecision_gate_a0_status",
        "apply_zdecision_gate_a0_delivery",
        "increment_zdecision_gate_a0_counter",
    ):
        _forbid_extra_tool_input(server, tool_name)
    return server


def _forbid_extra_tool_input(server: FastMCP, tool_name: str) -> None:
    tool = server._tool_manager.get_tool(tool_name)
    if tool is None:
        raise RuntimeError("Gate A0 tool registration failed")
    argument_model = tool.fn_metadata.arg_model
    argument_model.model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )
    argument_model.model_rebuild(force=True)
    tool.parameters = argument_model.model_json_schema(by_alias=True)


CARD_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZDecision Gate A0</title>
<style>
:root{color-scheme:light dark;font-family:ui-sans-serif,system-ui,sans-serif}
body{margin:0;background:#111820;color:#dce7f5}main{padding:18px}
h1{font-size:19px;margin:0 0 12px}.result{padding:12px;background:#18222d;border-left:3px solid #4f84d8}
#gate-state{font-weight:700}#gate-status,#gate-receipt{font-size:12px;color:#9fb0c3;overflow-wrap:anywhere}
button{width:100%;margin-top:14px;padding:11px;border:0;background:#2f73d9;color:white;font-weight:700}
button:disabled{background:#26313d;color:#75869a}
</style></head>
<body><main><h1>ZDecision Gate A0</h1><section class="result" aria-live="polite">
<div id="gate-state">正在初始化</div><p id="gate-status">只会交付测试决策。</p><p id="gate-receipt"></p>
</section><button id="enable-recall" type="button" disabled>启用本任务决策召回</button></main>
<script>
(() => {
  "use strict";
  const PROTOCOL = "2026-01-26";
  const HANDOFF = "recall-handoff-v1";
  const ATTEMPT = /^attempt_[0-9a-f]{32}$/;
  const DELIVERY = /^delivery_[0-9a-f]{32}$/;
  const pending = new Map();
  let nextId = 1;
  let initialized = false;
  let renderAccepted = false;
  let recoveryStarted = false;
  let actionStarted = false;
  let supportsTools = false;
  let supportsContext = false;
  let attemptId = null;
  const state = document.getElementById("gate-state");
  const status = document.getElementById("gate-status");
  const receipt = document.getElementById("gate-receipt");
  const button = document.getElementById("enable-recall");
  function object(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function request(method, params, timeoutMs) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error("timeout")); }, timeoutMs);
      pending.set(id, { resolve, reject, timer });
      window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    });
  }
  function notify(method, params) {
    window.parent.postMessage({ jsonrpc: "2.0", method, params }, "*");
  }
  function fail(text) { state.textContent = "验证失败"; status.textContent = text; button.disabled = true; }
  function validResult(value, expected) {
    const meta = object(value?._meta) ? value._meta : {};
    return object(value) && object(value.structuredContent)
      && value.structuredContent.protocol_version === HANDOFF
      && value.structuredContent.state === expected
      && meta["zdecision/attempt_id"] === attemptId;
  }
  function acceptRender(value) {
    const received = object(value?._meta) ? value._meta["zdecision/attempt_id"] : null;
    if (!object(value?.structuredContent)
      || value.structuredContent.protocol_version !== HANDOFF
      || !ATTEMPT.test(received || "")) { fail("卡片绑定无效。"); return; }
    if (renderAccepted && received !== attemptId) { fail("卡片绑定发生变化。"); return; }
    attemptId = received; renderAccepted = true; recover();
  }
  async function recover() {
    if (recoveryStarted || !initialized || !renderAccepted || !supportsTools) return;
    recoveryStarted = true;
    try {
      const value = await request("tools/call", {
        name: "get_zdecision_gate_a0_status", arguments: { attempt_id: attemptId }
      }, 3000);
      if (!object(value?.structuredContent) || value.structuredContent.protocol_version !== HANDOFF) {
        fail("无法恢复权威状态。"); return;
      }
      if (value.structuredContent.state === "pending_confirmation" && validResult(value, "pending_confirmation")) {
        state.textContent = "准备就绪"; status.textContent = "点击一次以交付两条测试决策。"; button.disabled = false; return;
      }
      if (["context_prepared", "host_delivered", "application_committed"].includes(value.structuredContent.state)
        && typeof value.structuredContent.receipt === "string") {
        state.textContent = "已恢复"; status.textContent = "已恢复同一权威回执。";
        receipt.textContent = value.structuredContent.receipt; button.disabled = true; return;
      }
      fail("恢复状态不匹配。");
    } catch { fail("无法读取权威状态。"); }
  }
  async function enable() {
    if (actionStarted || button.disabled || !supportsTools || !supportsContext) return;
    actionStarted = true; button.disabled = true; state.textContent = "正在交付";
    let enabled;
    try {
      enabled = await request("tools/call", {
        name: "enable_zdecision_gate_a0_delivery", arguments: { attempt_id: attemptId }
      }, 5000);
    } catch { state.textContent = "结果未知"; status.textContent = "不会自动重试。"; return; }
    if (!validResult(enabled, "context_prepared")
      || !object(enabled.structuredContent.snapshot)
      || enabled.structuredContent.snapshot.protocol_version !== HANDOFF
      || !DELIVERY.test(enabled.structuredContent.snapshot.delivery_id || "")
      || !Array.isArray(enabled.structuredContent.snapshot.decisions)
      || enabled.structuredContent.snapshot.decisions.length !== 2
      || typeof enabled.structuredContent.receipt !== "string") {
      fail("交付快照无效。"); return;
    }
    receipt.textContent = enabled.structuredContent.receipt;
    try {
      await request("ui/update-model-context", { content: [{
        type: "text", text: JSON.stringify(enabled.structuredContent.snapshot)
      }] }, 5000);
    } catch { state.textContent = "上下文失败"; status.textContent = "不会自动重试。"; return; }
    let acknowledged;
    try {
      acknowledged = await request("tools/call", {
        name: "ack_zdecision_gate_a0_delivery",
        arguments: { delivery_id: enabled.structuredContent.snapshot.delivery_id }
      }, 5000);
    } catch { state.textContent = "回执未知"; status.textContent = "重新打开可只读恢复。"; return; }
    if (!validResult(acknowledged, "host_delivered")
      || acknowledged.structuredContent.receipt !== enabled.structuredContent.receipt) {
      fail("确认回执不匹配。"); return;
    }
    state.textContent = "已交付";
    status.textContent = "请保留 App 附件并发送下一条原生消息。";
  }
  button.addEventListener("click", enable);
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent || !object(event.data) || event.data.jsonrpc !== "2.0") return;
    const value = event.data;
    if (Object.hasOwn(value, "id")) {
      const waiter = pending.get(value.id); if (!waiter) return;
      pending.delete(value.id); clearTimeout(waiter.timer);
      if (value.error) waiter.reject(value.error); else waiter.resolve(value.result); return;
    }
    if (value.method === "ui/notifications/tool-result") acceptRender(value.params);
  }, { passive: true });
  request("ui/initialize", {
    appInfo: { name: "zdecision-gate-a0", version: "1" },
    appCapabilities: {}, protocolVersion: PROTOCOL
  }, 3000).then((value) => {
    const capabilities = object(value) && object(value.hostCapabilities) ? value.hostCapabilities : {};
    supportsTools = object(capabilities.serverTools);
    supportsContext = object(capabilities.updateModelContext) && object(capabilities.updateModelContext.text);
    initialized = true; notify("ui/notifications/initialized", {});
    if (!supportsTools || !supportsContext) { fail("宿主缺少已验证的交付能力。"); return; }
    recover();
  }).catch(() => fail("MCP Apps 初始化失败。"));
})();
</script></body></html>
'''


def create(root: Path, repository: Path) -> dict[str, object]:
    if root.exists() and any(root.iterdir()):
        raise ValueError("Gate A0 root must be fresh")
    if not repository.is_absolute() or not repository.is_dir():
        raise ValueError("enabled repository must be an existing absolute directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    selector = f"zdecision-gate-a0-{secrets.token_hex(4)}"
    _write_json(
        root / _MARKER_NAME,
        {
            "protocol_version": PROTOCOL_VERSION,
            "root": str(root),
            "repository": str(repository.resolve()),
            "selector": selector,
        },
    )
    python = os.path.abspath(sys.executable)
    harness = str(Path(__file__).resolve())
    _write_json(
        root / "plugin/.codex-plugin/plugin.json",
        {
            "name": selector,
            "version": "0.1.0+codex.gate-a0-disposable",
            "description": "Disposable ZDecision next-native-message Gate A0",
            "author": {"name": "ZDecision"},
            "mcpServers": "./.mcp.json",
            "hooks": "./hooks/hooks.json",
            "interface": {
                "displayName": "ZDecision Gate A0",
                "shortDescription": "Disposable Decision handoff verification",
                "longDescription": (
                    "A disposable test-only Decision delivery and application vertical."
                ),
                "developerName": "ZDecision",
                "category": "Developer Tools",
                "capabilities": ["Interactive"],
                "defaultPrompt": ["运行 ZDecision Gate A0"],
            },
        },
    )
    _write_json(
        root / "plugin/.mcp.json",
        {
            "mcpServers": {
                "zdecision-gate-a0": {
                    "command": python,
                    "args": [harness, "mcp", "--root", str(root)],
                }
            }
        },
    )
    hook_command = " ".join(
        shlex.quote(part)
        for part in (python, harness, "hook", "--root", str(root))
    )
    _write_json(
        root / "plugin/hooks/hooks.json",
        {
            "description": "Bind and guard only the disposable Gate A0 tools.",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": (
                            "mcp__zdecision_gate_a0__show_zdecision_gate_a0|"
                            "mcp__zdecision_gate_a0__apply_zdecision_gate_a0_delivery|"
                            "mcp__zdecision_gate_a0__increment_zdecision_gate_a0_counter"
                        ),
                        "hooks": [
                            {"type": "command", "command": hook_command, "timeout": 3}
                        ],
                    }
                ]
            },
        },
    )
    _write_text(root / "plugin/static/recall-gate-a0-v1.html", CARD_HTML)
    _write_json(
        root / "marketplace/.agents/plugins/marketplace.json",
        {
            "name": f"{selector}-marketplace",
            "interface": {"displayName": "ZDecision Gate A0 Disposable"},
            "plugins": [
                {
                    "name": selector,
                    "source": {"source": "local", "path": str(root / "plugin")},
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Productivity",
                }
            ],
        },
    )
    store = GateA0Store(root)
    store.close()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "selector": selector,
        "plugin_root": str(root / "plugin"),
        "marketplace_root": str(root / "marketplace"),
    }


def cleanup(root: Path) -> dict[str, object]:
    _configuration(root)
    shutil.rmtree(root)
    return {"protocol_version": PROTOCOL_VERSION, "removed": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recall-gate-a0-disposable")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--root", required=True)
    create_parser.add_argument("--repository", required=True)
    for command in ("hook", "mcp", "inspect", "cleanup"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "create":
        root = _root(arguments.root, require_marker=False)
        repository = Path(arguments.repository)
        result = create(root, repository)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    root = _root(arguments.root, require_marker=True)
    if arguments.command == "hook":
        print(json.dumps(run_hook(root), ensure_ascii=False, sort_keys=True))
        return 0
    if arguments.command == "mcp":
        store = GateA0Store(root)
        try:
            create_server(store).run(transport="stdio")
        finally:
            store.close()
        return 0
    if arguments.command == "inspect":
        store = GateA0Store(root)
        try:
            print(json.dumps(store.inspect(), ensure_ascii=False, sort_keys=True))
        finally:
            store.close()
        return 0
    if arguments.command == "cleanup":
        print(json.dumps(cleanup(root), ensure_ascii=False, sort_keys=True))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
