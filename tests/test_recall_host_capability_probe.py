"""Tests for the disposable Recall MCP Apps host-capability probe."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from collections import deque
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from zdecision.agent.host_capability_probe import HostCapabilityProbeStore


NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class TokenSequence:
    def __init__(self, *values: str) -> None:
        self.values = deque(values)

    def __call__(self) -> str:
        return self.values.popleft()


class HostCapabilityProbeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "host-probe" / "probe.sqlite3"
        )
        self.clock = MutableClock()
        self.tokens = TokenSequence(
            "a" * 32,
            "b" * 32,
            "c" * 32,
            "d" * 32,
            "e" * 32,
            "f" * 32,
            "g" * 32,
            "h" * 32,
            "i" * 32,
        )
        self.store = HostCapabilityProbeStore.open(
            self.database_path,
            clock=self.clock,
            token=self.tokens,
        )
        self.addCleanup(self.store.close)

    def test_create_commit_and_replay_return_one_authoritative_receipt(self) -> None:
        created = self.store.create()

        committed = self.store.commit(created.probe_id)
        replay = self.store.commit(created.probe_id)

        self.assertEqual("ready", created.state)
        self.assertIsNotNone(committed)
        self.assertEqual("committed", committed.state)
        self.assertEqual(committed, replay)
        self.assertEqual(created.marker, committed.marker)
        self.assertEqual(created.receipt, committed.receipt)
        self.assertEqual(
            NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            committed.committed_at,
        )

    def test_reopen_recovers_committed_probe(self) -> None:
        created = self.store.create()
        committed = self.store.commit(created.probe_id)
        self.store.close()

        reopened = HostCapabilityProbeStore.open(
            self.database_path,
            clock=self.clock,
            token=self.tokens,
        )
        self.addCleanup(reopened.close)

        self.assertEqual(committed, reopened.get(created.probe_id))

    def test_unknown_malformed_and_expired_ids_do_not_commit(self) -> None:
        self.assertIsNone(self.store.commit("not-a-probe"))
        self.assertIsNone(self.store.commit("probe_" + "!" * 32))
        created = self.store.create()
        self.clock.now = datetime.fromisoformat(
            created.expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)

        expired = self.store.get(created.probe_id)

        self.assertIsNotNone(expired)
        self.assertEqual("expired", expired.state)
        self.assertIsNone(self.store.commit(created.probe_id))
        self.assertEqual("expired", self.store.get(created.probe_id).state)

    def test_store_bytes_exclude_business_sentinels(self) -> None:
        self.store.create()

        serialized = self.database_path.read_bytes().lower()

        for sentinel in (
            b"private_prompt_sentinel",
            b"private_transcript_sentinel",
            b"private_decision_sentinel",
            b"private_repository_sentinel",
        ):
            self.assertNotIn(sentinel, serialized)

    def test_two_connections_commit_once_and_replay_the_same_timestamp(self) -> None:
        created = self.store.create()
        other_clock = MutableClock(NOW + timedelta(seconds=5))
        other = HostCapabilityProbeStore.open(
            self.database_path,
            clock=other_clock,
            token=TokenSequence("j" * 32, "k" * 32, "l" * 32),
        )
        self.addCleanup(other.close)

        first = other.commit(created.probe_id)
        self.clock.now = NOW + timedelta(seconds=10)
        replay = self.store.commit(created.probe_id)

        self.assertEqual(first, replay)
        self.assertEqual(
            (NOW + timedelta(seconds=5))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            replay.committed_at,
        )

    def test_clock_must_return_a_utc_aware_datetime(self) -> None:
        for invalid_now in (
            datetime(2026, 8, 10, 0, 0),
            datetime(2026, 8, 10, 1, 0, tzinfo=timezone(timedelta(hours=1))),
            "2026-08-10T00:00:00Z",
        ):
            with self.subTest(invalid_now=invalid_now):
                store = HostCapabilityProbeStore.open(
                    Path(self.temporary_directory.name)
                    / f"invalid-{len(str(invalid_now))}"
                    / "probe.sqlite3",
                    clock=lambda invalid_now=invalid_now: invalid_now,
                    token=TokenSequence("m" * 32, "n" * 32, "o" * 32),
                )
                self.addCleanup(store.close)
                with self.assertRaises(ValueError):
                    store.create()

    def test_duplicate_generated_id_is_retried_without_overwriting(self) -> None:
        first = self.store.create()
        duplicate_then_fresh = TokenSequence(
            "a" * 32,
            "d" * 32,
            "e" * 32,
            "f" * 32,
            "g" * 32,
            "h" * 32,
        )
        other = HostCapabilityProbeStore.open(
            self.database_path,
            clock=self.clock,
            token=duplicate_then_fresh,
        )
        self.addCleanup(other.close)

        second = other.create()

        self.assertEqual("probe_" + "a" * 32, first.probe_id)
        self.assertEqual("probe_" + "f" * 32, second.probe_id)
        self.assertEqual(first, self.store.get(first.probe_id))

    def test_closed_store_rejects_all_operations(self) -> None:
        created = self.store.create()
        self.store.close()

        for operation in (
            self.store.create,
            lambda: self.store.get(created.probe_id),
            lambda: self.store.commit(created.probe_id),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(RuntimeError):
                    operation()

    def test_database_and_parent_are_owner_only(self) -> None:
        self.assertEqual(0o700, os.stat(self.database_path.parent).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(self.database_path).st_mode & 0o777)

    def test_generated_coordinates_have_exact_bounded_shapes(self) -> None:
        created = self.store.create()

        self.assertEqual(38, len(created.probe_id))
        self.assertEqual(53, len(created.marker))
        self.assertEqual(40, len(created.receipt))
        self.assertRegex(created.probe_id, r"^probe_[A-Za-z0-9_-]{32}$")
        self.assertRegex(created.marker, r"^ZDECISION_HOST_PROBE_[A-Za-z0-9_-]{32}$")
        self.assertRegex(created.receipt, r"^receipt_[A-Za-z0-9_-]{32}$")
        self.assertEqual(1, created.probe_version)
        self.assertEqual(
            (NOW + timedelta(hours=24))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            created.expires_at,
        )

    def test_schema_contains_only_the_probe_table(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }

        self.assertEqual({"recall_host_capability_probes"}, tables)


if __name__ == "__main__":
    unittest.main()
