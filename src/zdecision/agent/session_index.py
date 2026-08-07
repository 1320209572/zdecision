"""Durable local Session boundaries for on-demand decision Capture."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.events import AgentEvent
from zdecision.app_server.models import FeasibilityModelProfile
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import CaptureScope


_CAPTURE_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EXCLUSION_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class FrozenSessionSource:
    request_id: str
    source_key: str
    repository_id: str
    session_id: str
    cwd: str
    lineage: str
    previous_handled_turn_id: str | None
    upper_turn_id: str
    source_fingerprint: str
    previous_handled_head_commit: str | None = None
    upper_head_commit: str | None = None
    previous_handled_event_id: str | None = None
    upper_stop_event_id: str | None = None


class RequestModelProfileConflict(Exception):
    """A Capture Request already froze a different model profile."""


class RequestModelProfileCorrupt(Exception):
    """The private Capture Request profile cannot be trusted."""


class SessionIndex:
    """A private SQLite index of changed Codex Session boundaries."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "SessionIndex":
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
                CREATE TABLE IF NOT EXISTS session_checkpoints (
                    source_key TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    lineage TEXT NOT NULL,
                    latest_turn_id TEXT NOT NULL,
                    latest_event_id TEXT NOT NULL,
                    latest_observed_at TEXT NOT NULL,
                    latest_source_fingerprint TEXT NOT NULL,
                    handled_turn_id TEXT,
                    handled_source_fingerprint TEXT,
                    handled_event_id TEXT,
                    latest_head_commit TEXT,
                    handled_head_commit TEXT,
                    excluded_reason TEXT,
                    UNIQUE(repository_id, session_id, lineage)
                );

                CREATE TABLE IF NOT EXISTS capture_request_freezes (
                    request_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    capture_scope TEXT NOT NULL CHECK(
                        capture_scope IN (
                            'current_session', 'all_valid_sessions'
                        )
                    ),
                    selected_session_id TEXT,
                    frozen_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    acknowledgement_digest TEXT,
                    model_profile_json TEXT
                );

                CREATE TABLE IF NOT EXISTS capture_request_sources (
                    request_id TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    lineage TEXT NOT NULL,
                    previous_handled_turn_id TEXT,
                    upper_turn_id TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    previous_handled_head_commit TEXT,
                    upper_head_commit TEXT,
                    previous_handled_event_id TEXT,
                    upper_stop_event_id TEXT,
                    state TEXT NOT NULL CHECK(
                        state IN ('frozen','excluded','acknowledged')
                    ),
                    excluded_reason TEXT,
                    frozen_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    acknowledgement_digest TEXT,
                    PRIMARY KEY(request_id, source_key)
                );

                CREATE INDEX IF NOT EXISTS session_checkpoints_repository
                    ON session_checkpoints(repository_id);
                CREATE INDEX IF NOT EXISTS capture_request_sources_request
                    ON capture_request_sources(request_id, source_key);
                """
            )
            freeze_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(capture_request_freezes)"
                ).fetchall()
            }
            if "capture_scope" not in freeze_columns:
                connection.execute(
                    "ALTER TABLE capture_request_freezes ADD COLUMN "
                    "capture_scope TEXT NOT NULL DEFAULT "
                    "'all_valid_sessions' CHECK(capture_scope IN "
                    "('current_session', 'all_valid_sessions'))"
                )
            if "selected_session_id" not in freeze_columns:
                connection.execute(
                    "ALTER TABLE capture_request_freezes ADD COLUMN "
                    "selected_session_id TEXT"
                )
            if "model_profile_json" not in freeze_columns:
                connection.execute(
                    "ALTER TABLE capture_request_freezes ADD COLUMN "
                    "model_profile_json TEXT"
                )
            checkpoint_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(session_checkpoints)"
                ).fetchall()
            }
            if "latest_head_commit" not in checkpoint_columns:
                connection.execute(
                    "ALTER TABLE session_checkpoints ADD COLUMN "
                    "latest_head_commit TEXT"
                )
            if "handled_head_commit" not in checkpoint_columns:
                connection.execute(
                    "ALTER TABLE session_checkpoints ADD COLUMN "
                    "handled_head_commit TEXT"
                )
            if "handled_event_id" not in checkpoint_columns:
                connection.execute(
                    "ALTER TABLE session_checkpoints ADD COLUMN "
                    "handled_event_id TEXT"
                )
            source_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(capture_request_sources)"
                ).fetchall()
            }
            if "previous_handled_head_commit" not in source_columns:
                connection.execute(
                    "ALTER TABLE capture_request_sources ADD COLUMN "
                    "previous_handled_head_commit TEXT"
                )
            if "upper_head_commit" not in source_columns:
                connection.execute(
                    "ALTER TABLE capture_request_sources ADD COLUMN "
                    "upper_head_commit TEXT"
                )
            if "previous_handled_event_id" not in source_columns:
                connection.execute(
                    "ALTER TABLE capture_request_sources ADD COLUMN "
                    "previous_handled_event_id TEXT"
                )
            if "upper_stop_event_id" not in source_columns:
                connection.execute(
                    "ALTER TABLE capture_request_sources ADD COLUMN "
                    "upper_stop_event_id TEXT"
                )
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def handled_turn(self, source_key: str) -> str | None:
        if not isinstance(source_key, str) or not source_key:
            raise ValueError("source_key is invalid")
        row = self._connection.execute(
            """
            SELECT handled_turn_id
            FROM session_checkpoints
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()
        return None if row is None else row["handled_turn_id"]

    def observe(self, event: AgentEvent) -> None:
        invocation = event.invocation
        if (
            invocation.event_name != "Stop"
            or invocation.repository_id is None
            or invocation.turn_id is None
            or invocation.worktree_root is None
        ):
            return
        _require_repository_id(invocation.repository_id)
        observed_at = _normalized_timestamp(invocation.occurred_at)
        lineage = _stable_value(
            "lin",
            {
                "branch": invocation.branch,
                "repository_id": invocation.repository_id,
                "worktree_root": invocation.worktree_root,
            },
        )
        source_key = _stable_value(
            "src",
            {
                "lineage": lineage,
                "repository_id": invocation.repository_id,
                "session_id": invocation.session_id,
            },
        )
        source_fingerprint = hashlib.sha256(
            canonical_json_bytes(
                {
                    "head_commit": invocation.head_commit,
                    "lineage": lineage,
                    "session_id": invocation.session_id,
                    "turn_id": invocation.turn_id,
                }
            )
        ).hexdigest()

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                """
                SELECT repository_id, session_id, lineage,
                       latest_observed_at, latest_event_id
                FROM session_checkpoints
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()
            if existing is not None:
                identity = (
                    existing["repository_id"],
                    existing["session_id"],
                    existing["lineage"],
                )
                expected = (
                    invocation.repository_id,
                    invocation.session_id,
                    lineage,
                )
                if identity != expected:
                    raise ValueError("Session source identity conflicts")
                current_position = (
                    existing["latest_observed_at"],
                    existing["latest_event_id"],
                )
                if (observed_at, event.event_id) <= current_position:
                    self._connection.commit()
                    return
                self._connection.execute(
                    """
                    UPDATE session_checkpoints
                    SET cwd = ?,
                        latest_turn_id = ?,
                        latest_event_id = ?,
                        latest_observed_at = ?,
                        latest_source_fingerprint = ?,
                        latest_head_commit = ?
                    WHERE source_key = ?
                    """,
                    (
                        invocation.cwd,
                        invocation.turn_id,
                        event.event_id,
                        observed_at,
                        source_fingerprint,
                        invocation.head_commit,
                        source_key,
                    ),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO session_checkpoints(
                        source_key, repository_id, session_id, cwd, lineage,
                        latest_turn_id, latest_event_id, latest_observed_at,
                        latest_source_fingerprint, handled_turn_id,
                        handled_source_fingerprint, latest_head_commit,
                        handled_head_commit, excluded_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL,
                              NULL)
                    """,
                    (
                        source_key,
                        invocation.repository_id,
                        invocation.session_id,
                        invocation.cwd,
                        lineage,
                        invocation.turn_id,
                        event.event_id,
                        observed_at,
                        source_fingerprint,
                        invocation.head_commit,
                    ),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def freeze_sources(
        self,
        request_id: str,
        repository_id: str,
        frozen_at: datetime,
        *,
        capture_scope: CaptureScope,
        selected_session_id: str | None = None,
    ) -> tuple[FrozenSessionSource, ...]:
        _require_capture_request_id(request_id)
        _require_repository_id(repository_id)
        if capture_scope not in ("current_session", "all_valid_sessions"):
            raise ValueError("capture_scope is invalid")
        if capture_scope == "current_session":
            if not isinstance(selected_session_id, str) or not selected_session_id:
                raise ValueError("selected_session_id is required")
        elif selected_session_id is not None:
            raise ValueError(
                "selected_session_id is invalid for all_valid_sessions"
            )
        timestamp = _normalized_datetime(frozen_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing_freeze = self._connection.execute(
                """
                SELECT repository_id, capture_scope, selected_session_id
                FROM capture_request_freezes
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing_freeze is not None:
                identity = (
                    existing_freeze["repository_id"],
                    existing_freeze["capture_scope"],
                    existing_freeze["selected_session_id"],
                )
                expected = (
                    repository_id,
                    capture_scope,
                    selected_session_id,
                )
                if identity != expected:
                    raise ValueError("Capture Request freeze identity conflicts")
                sources = self._request_sources(request_id)
                self._connection.commit()
                return sources

            self._connection.execute(
                """
                INSERT INTO capture_request_freezes(
                    request_id, repository_id, capture_scope,
                    selected_session_id, frozen_at,
                    acknowledged_at, acknowledgement_digest
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    request_id,
                    repository_id,
                    capture_scope,
                    selected_session_id,
                    timestamp,
                ),
            )
            changed_clause = """
                excluded_reason IS NULL
                AND (
                    handled_turn_id IS NULL
                    OR handled_source_fingerprint IS NULL
                    OR handled_turn_id <> latest_turn_id
                    OR handled_source_fingerprint <> latest_source_fingerprint
                )
            """
            if capture_scope == "current_session":
                checkpoints = self._connection.execute(
                    f"""
                    SELECT source_key, repository_id, session_id, cwd, lineage,
                           handled_turn_id, latest_turn_id,
                           latest_source_fingerprint, handled_head_commit,
                           latest_head_commit, handled_event_id, latest_event_id
                    FROM session_checkpoints
                    WHERE repository_id = ?
                      AND session_id = ?
                      AND {changed_clause}
                    ORDER BY latest_observed_at DESC, latest_event_id DESC
                    LIMIT 1
                    """,
                    (repository_id, selected_session_id),
                ).fetchall()
            else:
                checkpoints = self._connection.execute(
                    f"""
                    SELECT source_key, repository_id, session_id, cwd, lineage,
                           handled_turn_id, latest_turn_id,
                           latest_source_fingerprint, handled_head_commit,
                           latest_head_commit, handled_event_id, latest_event_id
                    FROM session_checkpoints
                    WHERE repository_id = ?
                      AND {changed_clause}
                    ORDER BY source_key
                    """,
                    (repository_id,),
                ).fetchall()
            for checkpoint in checkpoints:
                self._connection.execute(
                    """
                    INSERT INTO capture_request_sources(
                        request_id, source_key, repository_id, session_id,
                        cwd, lineage, previous_handled_turn_id, upper_turn_id,
                        source_fingerprint, previous_handled_head_commit,
                        upper_head_commit, previous_handled_event_id,
                        upper_stop_event_id, state, excluded_reason, frozen_at,
                        acknowledged_at, acknowledgement_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'frozen', NULL,
                              ?, NULL, NULL)
                    """,
                    (
                        request_id,
                        checkpoint["source_key"],
                        checkpoint["repository_id"],
                        checkpoint["session_id"],
                        checkpoint["cwd"],
                        checkpoint["lineage"],
                        checkpoint["handled_turn_id"],
                        checkpoint["latest_turn_id"],
                        checkpoint["latest_source_fingerprint"],
                        checkpoint["handled_head_commit"],
                        checkpoint["latest_head_commit"],
                        self._legacy_handled_event_id(checkpoint),
                        checkpoint["latest_event_id"],
                        timestamp,
                    ),
                )
            sources = self._request_sources(request_id)
            self._connection.commit()
            return sources
        except Exception:
            self._connection.rollback()
            raise

    def mark_excluded(self, request_id: str, source_key: str, reason: str) -> None:
        _require_capture_request_id(request_id)
        if not isinstance(source_key, str) or not source_key:
            raise ValueError("source_key is invalid")
        if not isinstance(reason, str) or _EXCLUSION_REASON.fullmatch(reason) is None:
            raise ValueError("Exclusion reason is invalid")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            source = self._connection.execute(
                """
                SELECT state, excluded_reason
                FROM capture_request_sources
                WHERE request_id = ? AND source_key = ?
                """,
                (request_id, source_key),
            ).fetchone()
            if source is None:
                raise ValueError("Frozen Session source does not exist")
            if source["state"] == "acknowledged":
                raise ValueError("Frozen Session source is already acknowledged")
            if source["excluded_reason"] is not None:
                if source["excluded_reason"] != reason:
                    raise ValueError("Frozen Session exclusion conflicts")
                self._connection.commit()
                return
            self._connection.execute(
                """
                UPDATE capture_request_sources
                SET state = 'excluded', excluded_reason = ?
                WHERE request_id = ? AND source_key = ?
                """,
                (reason, request_id, source_key),
            )
            self._connection.execute(
                """
                UPDATE session_checkpoints
                SET excluded_reason = ?
                WHERE source_key = ?
                """,
                (reason, source_key),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def request_model_profile(
        self, request_id: str
    ) -> FeasibilityModelProfile | None:
        _require_capture_request_id(request_id)
        row = self._connection.execute(
            """
            SELECT model_profile_json
            FROM capture_request_freezes
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise RequestModelProfileConflict(
                "Capture Request has not been frozen"
            )
        encoded = row["model_profile_json"]
        if encoded is None:
            return None
        return _parse_model_profile_json(encoded)

    def freeze_request_model_profile(
        self,
        request_id: str,
        profile: FeasibilityModelProfile,
    ) -> FeasibilityModelProfile:
        _require_capture_request_id(request_id)
        if not isinstance(profile, FeasibilityModelProfile):
            raise TypeError("profile must be a FeasibilityModelProfile")
        encoded = canonical_json_bytes(_model_profile_dict(profile)).decode(
            "utf-8"
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                """
                SELECT model_profile_json
                FROM capture_request_freezes
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                raise RequestModelProfileConflict(
                    "Capture Request has not been frozen"
                )
            existing = row["model_profile_json"]
            if existing is not None:
                stored = _parse_model_profile_json(existing)
                if existing != encoded:
                    raise RequestModelProfileConflict(
                        "Capture Request model profile conflicts"
                    )
                self._connection.commit()
                return stored
            self._connection.execute(
                """
                UPDATE capture_request_freezes
                SET model_profile_json = ?
                WHERE request_id = ? AND model_profile_json IS NULL
                """,
                (encoded, request_id),
            )
            self._connection.commit()
            return profile
        except Exception:
            self._connection.rollback()
            raise

    def acknowledge(
        self,
        request_id: str,
        batch_digest: str,
        acknowledged_at: datetime,
    ) -> None:
        _require_capture_request_id(request_id)
        if not isinstance(batch_digest, str) or _DIGEST.fullmatch(batch_digest) is None:
            raise ValueError("batch_digest is invalid")
        timestamp = _normalized_datetime(acknowledged_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            freeze = self._connection.execute(
                """
                SELECT acknowledgement_digest
                FROM capture_request_freezes
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if freeze is None:
                raise ValueError("Capture Request has not been frozen")
            if freeze["acknowledgement_digest"] is not None:
                if freeze["acknowledgement_digest"] != batch_digest:
                    raise ValueError("Capture Request acknowledgement conflicts")
                self._connection.commit()
                return
            sources = self._connection.execute(
                """
                SELECT source_key, upper_turn_id, source_fingerprint,
                       upper_head_commit, upper_stop_event_id
                FROM capture_request_sources
                WHERE request_id = ?
                ORDER BY source_key
                """,
                (request_id,),
            ).fetchall()
            for source in sources:
                self._connection.execute(
                    """
                    UPDATE session_checkpoints
                    SET handled_turn_id = ?,
                        handled_source_fingerprint = ?,
                        handled_head_commit = ?,
                        handled_event_id = ?
                    WHERE source_key = ?
                    """,
                    (
                        source["upper_turn_id"],
                        source["source_fingerprint"],
                        source["upper_head_commit"],
                        source["upper_stop_event_id"],
                        source["source_key"],
                    ),
                )
            self._connection.execute(
                """
                UPDATE capture_request_sources
                SET state = 'acknowledged',
                    acknowledged_at = ?,
                    acknowledgement_digest = ?
                WHERE request_id = ?
                """,
                (timestamp, batch_digest, request_id),
            )
            self._connection.execute(
                """
                UPDATE capture_request_freezes
                SET acknowledged_at = ?, acknowledgement_digest = ?
                WHERE request_id = ?
                """,
                (timestamp, batch_digest, request_id),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _request_sources(
        self, request_id: str
    ) -> tuple[FrozenSessionSource, ...]:
        rows = self._connection.execute(
            """
            SELECT request_id, source_key, repository_id, session_id, cwd,
                   lineage, previous_handled_turn_id, upper_turn_id,
                   source_fingerprint, previous_handled_head_commit,
                   upper_head_commit, previous_handled_event_id,
                   upper_stop_event_id
            FROM capture_request_sources
            WHERE request_id = ? AND excluded_reason IS NULL
            ORDER BY source_key
            """,
            (request_id,),
        ).fetchall()
        return tuple(
            FrozenSessionSource(
                request_id=row["request_id"],
                source_key=row["source_key"],
                repository_id=row["repository_id"],
                session_id=row["session_id"],
                cwd=row["cwd"],
                lineage=row["lineage"],
                previous_handled_turn_id=row["previous_handled_turn_id"],
                upper_turn_id=row["upper_turn_id"],
                source_fingerprint=row["source_fingerprint"],
                previous_handled_head_commit=(
                    row["previous_handled_head_commit"]
                ),
                upper_head_commit=row["upper_head_commit"],
                previous_handled_event_id=row["previous_handled_event_id"],
                upper_stop_event_id=row["upper_stop_event_id"],
            )
            for row in rows
        )

    def _legacy_handled_event_id(self, checkpoint: sqlite3.Row) -> str | None:
        """Resolve only an exact old Stop boundary; never infer one by recency."""

        if checkpoint["handled_event_id"] is not None:
            return checkpoint["handled_event_id"]
        if checkpoint["handled_turn_id"] is None:
            return None
        events_table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
        if events_table is None:
            return None
        matches = self._connection.execute(
            """
            SELECT event_id
            FROM events
            WHERE event_type = 'Stop'
              AND session_id = ?
              AND cwd = ?
              AND repository_id = ?
              AND turn_id = ?
            ORDER BY rowid
            LIMIT 2
            """,
            (
                checkpoint["session_id"],
                checkpoint["cwd"],
                checkpoint["repository_id"],
                checkpoint["handled_turn_id"],
            ),
        ).fetchall()
        return matches[0]["event_id"] if len(matches) == 1 else None


class SessionIndexEventProcessor:
    def __init__(self, index: SessionIndex) -> None:
        self.index = index

    def process(self, event: AgentEvent) -> None:
        self.index.observe(event)


def _stable_value(prefix: str, payload: object) -> str:
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _model_profile_dict(profile: FeasibilityModelProfile) -> dict[str, str]:
    return {
        "profile_id": profile.profile_id,
        "model_id": profile.model_id,
        "reasoning_effort": profile.reasoning_effort,
        "discovery_digest": profile.discovery_digest,
        "discovered_at": profile.discovered_at,
    }


def _parse_model_profile_json(value: object) -> FeasibilityModelProfile:
    if not isinstance(value, str):
        raise RequestModelProfileCorrupt(
            "Capture Request model profile is corrupt"
        )
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise RequestModelProfileCorrupt(
            "Capture Request model profile is corrupt"
        ) from None
    fields = {
        "profile_id",
        "model_id",
        "reasoning_effort",
        "discovery_digest",
        "discovered_at",
    }
    if not isinstance(parsed, dict) or set(parsed) != fields:
        raise RequestModelProfileCorrupt(
            "Capture Request model profile is corrupt"
        )
    if canonical_json_bytes(parsed).decode("utf-8") != value:
        raise RequestModelProfileCorrupt(
            "Capture Request model profile is not canonical"
        )
    try:
        profile = FeasibilityModelProfile(**parsed)
        derived = FeasibilityModelProfile.create(
            model_id=profile.model_id,
            reasoning_effort=profile.reasoning_effort,
            discovery_digest=profile.discovery_digest,
            discovered_at=profile.discovered_at,
        )
    except (TypeError, ValueError):
        raise RequestModelProfileCorrupt(
            "Capture Request model profile is corrupt"
        ) from None
    if derived.profile_id != profile.profile_id:
        raise RequestModelProfileCorrupt(
            "Capture Request model profile identity is corrupt"
        )
    return profile


def _require_capture_request_id(value: str) -> None:
    if not isinstance(value, str) or _CAPTURE_REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id is invalid")


def _require_repository_id(value: str) -> None:
    if not isinstance(value, str) or _REPOSITORY_ID.fullmatch(value) is None:
        raise ValueError("repository_id is invalid")


def _normalized_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _normalized_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Observed timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Observed timestamp is invalid") from error
    return _normalized_datetime(parsed)
