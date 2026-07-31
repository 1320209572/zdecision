"""Singleton background processing for the device-local Agent."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import AgentEvent


_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class EventProcessor(Protocol):
    def process(self, event: AgentEvent) -> None: ...


class SyncPoller(Protocol):
    def poll(self, current_cursor: int) -> int: ...


class RetryableWorkerError(Exception):
    """A bounded event-processing failure that may be retried."""

    def __init__(self, code: str) -> None:
        if _FAILURE_CODE.fullmatch(code) is None:
            raise ValueError("Retryable Worker failure code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WorkerConfig:
    claim_limit: int = 32
    processing_lease_seconds: float = 30.0
    session_lease_seconds: float = 120.0
    poll_interval_seconds: float = 60.0
    idle_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.claim_limit, int)
            or isinstance(self.claim_limit, bool)
            or self.claim_limit <= 0
        ):
            raise ValueError("claim_limit must be positive")
        for name in (
            "processing_lease_seconds",
            "session_lease_seconds",
            "poll_interval_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.idle_grace_seconds < 0:
            raise ValueError("idle_grace_seconds cannot be negative")


@dataclass(frozen=True)
class WorkerCycle:
    claimed: int
    consumed: int
    deferred: int
    failed_retryable: int
    sync_cursor: int
    active_sessions: int


class ProbeSyncPoller:
    """Local feasibility poller used until the central sync client exists."""

    def poll(self, current_cursor: int) -> int:
        return current_cursor + 1


class Worker:
    def __init__(
        self,
        *,
        database: AgentDatabase,
        processor: EventProcessor,
        sync_poller: SyncPoller,
        lock_path: Path,
        config: WorkerConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.database = database
        self.processor = processor
        self.sync_poller = sync_poller
        self.lock_path = Path(lock_path)
        self.config = config or WorkerConfig()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper or time.sleep

    def run_once(self, now: datetime) -> WorkerCycle:
        _require_aware(now)
        self.database.requeue_expired_claims(now)
        events = self.database.claim_events(
            now,
            limit=self.config.claim_limit,
            processing_lease_seconds=self.config.processing_lease_seconds,
        )
        consumed = 0
        failed_retryable = 0
        for event in events:
            try:
                self.processor.process(event)
            except RetryableWorkerError as error:
                if self.database.fail_event(
                    event.event_id,
                    failure_code=error.code,
                    retry_at=now
                    + timedelta(seconds=self.config.processing_lease_seconds),
                ):
                    failed_retryable += 1
            except Exception:
                if self.database.fail_event(
                    event.event_id,
                    failure_code="event_processor_error",
                    retry_at=now
                    + timedelta(seconds=self.config.processing_lease_seconds),
                ):
                    failed_retryable += 1
            else:
                if self.database.consume_event(event.event_id):
                    consumed += 1

        active_sessions = len(self.database.active_session_leases(now))
        sync_cursor, last_polled_at = self.database.sync_probe()
        poll_due = last_polled_at is None or now >= last_polled_at + timedelta(
            seconds=self.config.poll_interval_seconds
        )
        if active_sessions and poll_due:
            try:
                next_cursor = self.sync_poller.poll(sync_cursor)
                sync_cursor = self.database.update_sync_probe(
                    expected_cursor=sync_cursor,
                    new_cursor=next_cursor,
                    updated_at=now,
                )
            except Exception:
                pass

        return WorkerCycle(
            claimed=len(events),
            consumed=consumed,
            deferred=0,
            failed_retryable=failed_retryable,
            sync_cursor=sync_cursor,
            active_sessions=active_sessions,
        )

    def run_until_idle(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_stream:
            try:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            owner_pid = os.getpid()
            try:
                idle_since: datetime | None = None
                while True:
                    now = self.clock()
                    _require_aware(now)
                    owner_lease = now + timedelta(
                        seconds=max(
                            self.config.processing_lease_seconds,
                            self.config.session_lease_seconds,
                            self.config.poll_interval_seconds,
                        )
                    )
                    self.database.set_worker_owner(
                        owner_pid, lease_expires_at=owner_lease
                    )
                    cycle = self.run_once(now)
                    if cycle.claimed:
                        idle_since = None
                        continue
                    if cycle.active_sessions or self.database.pending_event_count():
                        idle_since = None
                        self.sleeper(self._next_wake_delay(now))
                        continue
                    if idle_since is None:
                        idle_since = now
                    remaining = self.config.idle_grace_seconds - (
                        now - idle_since
                    ).total_seconds()
                    if remaining <= 0:
                        return
                    self.sleeper(min(remaining, 0.1))
            finally:
                self.database.clear_worker_owner(owner_pid)
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    def _next_wake_delay(self, now: datetime) -> float:
        candidates = [1.0]
        next_event = self.database.next_event_due_at(now)
        if next_event is not None:
            candidates.append(max(0.0, (next_event - now).total_seconds()))
        active_leases = self.database.active_session_leases(now)
        if active_leases:
            candidates.append(
                max(
                    0.0,
                    min((lease.expires_at - now).total_seconds() for lease in active_leases),
                )
            )
            _, last_polled_at = self.database.sync_probe()
            if last_polled_at is None:
                candidates.append(0.0)
            else:
                next_poll = last_polled_at + timedelta(
                    seconds=self.config.poll_interval_seconds
                )
                candidates.append(max(0.0, (next_poll - now).total_seconds()))
        return max(0.01, min(candidates))


def wake_worker(database_path: Path) -> None:
    """Launch one detached contender; the process-level lock picks the owner."""

    path = Path(database_path)
    if not path.is_absolute():
        return
    environment = os.environ.copy()
    environment["ZDECISION_STATE_DIR"] = str(path.parent.parent)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "zdecision.agent.cli", "worker"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=environment,
        )
    except OSError:
        return


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Worker time must be timezone-aware")
