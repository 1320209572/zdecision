"""Private durable bindings from rendered controls to local action intents."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zdecision.sync.contracts import CaptureScope


_CONTROL_ID = re.compile(r"^ctl_[0-9a-f]{32}$")
_CLIENT_ACTION_ID = re.compile(
    r"^codex_action_[A-Za-z0-9][A-Za-z0-9._:-]{0,114}$"
)
_CENTRAL_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_SCOPES = frozenset(("current_session", "all_valid_sessions"))


@dataclass(frozen=True)
class ControlBinding:
    control_id: str
    session_id: str
    render_turn_id: str
    cwd: str
    repository_id: str
    product_id: str
    created_at: str
    expires_at: str
    chosen_scope: CaptureScope | None
    client_action_id: str | None
    central_request_id: str | None


class ControlBindingError(Exception):
    """Base class for rejected local control transitions."""


class ControlBindingNotFound(ControlBindingError, LookupError):
    """The trusted local control ID does not exist."""


class ControlBindingExpired(ControlBindingError):
    """An unused local control passed its fixed lifetime."""


class ControlRepositoryMismatch(ControlBindingError):
    """The click repository does not own the rendered control."""


class ControlScopeConflict(ControlBindingError):
    """A replay attempted to replace the first chosen scope."""


class ControlActionConflict(ControlBindingError):
    """A client action ID already belongs to another control."""


class ControlRequestConflict(ControlBindingError):
    """A central request attachment conflicts with frozen local state."""


class ControlBindingStore:
    """SQLite store containing only trusted local control metadata."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "ControlBindingStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_bindings (
                    control_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    render_turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    chosen_scope TEXT CHECK(
                        chosen_scope IS NULL OR
                        chosen_scope IN (
                            'current_session', 'all_valid_sessions'
                        )
                    ),
                    client_action_id TEXT UNIQUE,
                    central_request_id TEXT UNIQUE,
                    CHECK(
                        (chosen_scope IS NULL AND client_action_id IS NULL) OR
                        (chosen_scope IS NOT NULL AND client_action_id IS NOT NULL)
                    )
                );
                """
            )
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def create_binding(
        self,
        *,
        session_id: str,
        render_turn_id: str,
        cwd: str,
        repository_id: str,
        product_id: str,
        created_at: datetime,
        expires_at: datetime,
        control_id: str,
    ) -> ControlBinding:
        control = _matching(control_id, _CONTROL_ID, "control_id")
        session = _nonempty(session_id, "session_id")
        turn = _nonempty(render_turn_id, "render_turn_id")
        working_directory = _absolute_path(cwd)
        repository = _matching(repository_id, _REPOSITORY_ID, "repository_id")
        product = _matching(product_id, _PRODUCT_ID, "product_id")
        created = _aware_utc(created_at, "created_at")
        expires = _aware_utc(expires_at, "expires_at")
        if expires != created + timedelta(minutes=15):
            raise ValueError("expires_at must be exactly 15 minutes after created_at")
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO control_bindings(
                        control_id, session_id, render_turn_id, cwd,
                        repository_id, product_id, created_at, expires_at,
                        chosen_scope, client_action_id, central_request_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        control,
                        session,
                        turn,
                        working_directory,
                        repository,
                        product,
                        _timestamp(created),
                        _timestamp(expires),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("control_id already exists") from error
        return self._required(control)

    def choose_scope(
        self,
        control_id: str,
        *,
        expected_repository_id: str,
        scope: CaptureScope,
        proposed_client_action_id: str,
        now: datetime,
    ) -> ControlBinding:
        control = _matching(control_id, _CONTROL_ID, "control_id")
        repository = _matching(
            expected_repository_id, _REPOSITORY_ID, "expected_repository_id"
        )
        if not isinstance(scope, str) or scope not in _SCOPES:
            raise ValueError("scope is invalid")
        current_time = _aware_utc(now, "now")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(control)
            if row is None:
                raise ControlBindingNotFound("control binding was not found")
            if row["repository_id"] != repository:
                raise ControlRepositoryMismatch("control repository does not match")
            if row["chosen_scope"] is not None:
                if row["chosen_scope"] != scope:
                    raise ControlScopeConflict("control scope is already frozen")
                binding = _binding(row)
                self._connection.commit()
                return binding
            if current_time >= _parse_timestamp(row["expires_at"]):
                raise ControlBindingExpired("unused control binding has expired")
            action = _matching(
                proposed_client_action_id,
                _CLIENT_ACTION_ID,
                "proposed_client_action_id",
            )
            try:
                self._connection.execute(
                    """
                    UPDATE control_bindings
                    SET chosen_scope = ?, client_action_id = ?
                    WHERE control_id = ?
                    """,
                    (scope, action, control),
                )
            except sqlite3.IntegrityError as error:
                raise ControlActionConflict(
                    "client action ID already belongs to another control"
                ) from error
            binding = self._required(control)
            self._connection.commit()
            return binding
        except Exception:
            self._connection.rollback()
            raise

    def attach_request(
        self,
        control_id: str,
        *,
        client_action_id: str,
        central_request_id: str,
    ) -> ControlBinding:
        control = _matching(control_id, _CONTROL_ID, "control_id")
        action = _matching(client_action_id, _CLIENT_ACTION_ID, "client_action_id")
        request = _matching(
            central_request_id, _CENTRAL_REQUEST_ID, "central_request_id"
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(control)
            if row is None:
                raise ControlBindingNotFound("control binding was not found")
            if row["client_action_id"] != action:
                raise ControlRequestConflict("client action ID does not match")
            if row["central_request_id"] is not None:
                if row["central_request_id"] != request:
                    raise ControlRequestConflict(
                        "central request ID is already attached"
                    )
                binding = _binding(row)
                self._connection.commit()
                return binding
            try:
                self._connection.execute(
                    """
                    UPDATE control_bindings
                    SET central_request_id = ?
                    WHERE control_id = ?
                    """,
                    (request, control),
                )
            except sqlite3.IntegrityError as error:
                raise ControlRequestConflict(
                    "central request ID already belongs to another control"
                ) from error
            binding = self._required(control)
            self._connection.commit()
            return binding
        except Exception:
            self._connection.rollback()
            raise

    def get(self, control_id: str) -> ControlBinding | None:
        control = _matching(control_id, _CONTROL_ID, "control_id")
        row = self._row(control)
        return None if row is None else _binding(row)

    def get_by_client_action_id(
        self, client_action_id: str
    ) -> ControlBinding | None:
        action = _matching(client_action_id, _CLIENT_ACTION_ID, "client_action_id")
        row = self._connection.execute(
            "SELECT * FROM control_bindings WHERE client_action_id = ?", (action,)
        ).fetchone()
        return None if row is None else _binding(row)

    def _row(self, control_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM control_bindings WHERE control_id = ?", (control_id,)
        ).fetchone()

    def _required(self, control_id: str) -> ControlBinding:
        row = self._row(control_id)
        if row is None:
            raise ControlBindingNotFound("control binding was not found")
        return _binding(row)


def _binding(row: sqlite3.Row) -> ControlBinding:
    return ControlBinding(
        control_id=row["control_id"],
        session_id=row["session_id"],
        render_turn_id=row["render_turn_id"],
        cwd=row["cwd"],
        repository_id=row["repository_id"],
        product_id=row["product_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        chosen_scope=row["chosen_scope"],
        client_action_id=row["client_action_id"],
        central_request_id=row["central_request_id"],
    )


def _matching(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    return value


def _absolute_path(value: object) -> str:
    path = _nonempty(value, "cwd")
    if not Path(path).is_absolute():
        raise ValueError("cwd must be absolute")
    return path


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
