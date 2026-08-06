"""Durable host-only state tests for trusted recall activation and gates."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.recall_host_state import (
    RecallGateConflict,
    RecallHostStore,
)
from zdecision.recall.session import TurnGateResult


NOW = datetime(2026, 8, 6, 3, 0, tzinfo=UTC)
SESSION_ID = "session-1"
TURN_ID = "turn-1"
ACTIVATION_ID = "activation-1"
GATE_ID = "gate-1"
ACTIVE_SET_DIGEST = "set-a"


def _result(
    *,
    disposition: str = "retrieve",
    intent_digest: str = "intent-a",
    context_epoch: int = 0,
    intent_epoch: int = 1,
) -> TurnGateResult:
    return TurnGateResult(
        disposition=disposition,  # type: ignore[arg-type]
        intent_digest=intent_digest,
        context_epoch=context_epoch,
        intent_epoch=intent_epoch,
        probe=None,
    )


class RecallHostStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "agent" / "state.sqlite3"
        self.store = RecallHostStore.open(self.path)
        self.addCleanup(self.store.close)

    def activate(
        self,
        *,
        session_id: str = SESSION_ID,
        turn_id: str = TURN_ID,
        binding_id: str = ACTIVATION_ID,
        cwd: str = "/tmp/recall/../recall",
    ):
        return self.store.bind_activation(
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
            binding_id=binding_id,
            now=NOW,
        )

    def begin_gate(
        self,
        *,
        session_id: str = SESSION_ID,
        turn_id: str = TURN_ID,
        gate_id: str = GATE_ID,
        context_epoch: int = 0,
        intent_epoch: int = 0,
        active_generation: int | None = None,
    ):
        return self.store.begin_turn_gate(
            session_id=session_id,
            turn_id=turn_id,
            context_epoch=context_epoch,
            intent_epoch=intent_epoch,
            active_generation=active_generation,
            gate_id=gate_id,
        )

    def commit_gate(
        self,
        *,
        session_id: str = SESSION_ID,
        turn_id: str = TURN_ID,
        gate_id: str = GATE_ID,
        result: TurnGateResult | None = None,
        active_set_digest: str | None = ACTIVE_SET_DIGEST,
    ):
        return self.store.commit_turn_gate(
            session_id=session_id,
            turn_id=turn_id,
            gate_id=gate_id,
            result=result or _result(),
            active_set_digest=active_set_digest,
        )

    def test_unselected_session_has_no_state_row(self) -> None:
        """This catches creating recall state before a trusted activation."""

        self.assertIsNone(self.store.get_session(SESSION_ID))

    def test_activation_is_frozen_to_its_trusted_binding(self) -> None:
        """This catches activation replay changing its Session, Turn, or CWD."""

        activated = self.activate()
        replay = self.activate()

        self.assertEqual("active", activated.state)
        self.assertEqual(TURN_ID, activated.authorization_turn_id)
        self.assertEqual("/tmp/recall", activated.cwd)
        self.assertEqual(activated, replay)
        with self.assertRaises(RecallGateConflict):
            self.activate(turn_id="turn-other")
        with self.assertRaises(RecallGateConflict):
            self.activate(session_id="session-other")
        with self.assertRaises(ValueError):
            self.activate(cwd="relative/path")

    def test_native_turn_gate_is_replay_safe_and_bound_to_current_epoch(self) -> None:
        """This catches reusing a gate across Sessions, Turns, or epochs."""

        self.activate()
        gate = self.begin_gate(active_generation=7)
        replay = self.begin_gate(active_generation=7)

        self.assertEqual(gate, replay)
        self.assertEqual(7, gate.active_generation)
        with self.assertRaises(RecallGateConflict):
            self.begin_gate(turn_id="turn-other", gate_id=GATE_ID)
        with self.assertRaises(RecallGateConflict):
            self.begin_gate(session_id="session-other", gate_id=GATE_ID)
        with self.assertRaises(RecallGateConflict):
            self.begin_gate(gate_id="gate-stale", context_epoch=1)

    def test_commit_updates_gate_and_session_together(self) -> None:
        """This catches committing a result without advancing durable session state."""

        self.activate()
        self.begin_gate()
        committed = self.commit_gate()
        session = self.store.get_session(SESSION_ID)

        self.assertEqual("committed", committed.state)
        self.assertIsNotNone(committed.result_digest)
        self.assertEqual(committed, self.store.require_committed_gate(SESSION_ID, TURN_ID))
        self.assertEqual(1, session.intent_epoch)
        self.assertEqual("intent-a", session.active_intent_digest)
        self.assertEqual(ACTIVE_SET_DIGEST, session.active_set_digest)
        self.assertEqual(TURN_ID, session.last_gate_turn_id)

    def test_invalid_result_blocks_current_turn_without_replacing_active_set(self) -> None:
        """This catches an invalid replay erasing prior trusted recall state."""

        self.activate()
        self.begin_gate()
        self.commit_gate()
        self.begin_gate(
            turn_id="turn-2", gate_id="gate-2", intent_epoch=1
        )

        with self.assertRaises(RecallGateConflict):
            self.commit_gate(
                turn_id="turn-2",
                gate_id="gate-2",
                result=_result(context_epoch=1, intent_epoch=2, intent_digest="bad"),
                active_set_digest="set-b",
            )

        session = self.store.get_session(SESSION_ID)
        self.assertEqual("intent-a", session.active_intent_digest)
        self.assertEqual(ACTIVE_SET_DIGEST, session.active_set_digest)
        with self.assertRaises(RecallGateConflict):
            self.store.require_committed_gate(SESSION_ID, "turn-2")

    def test_compact_or_clear_replay_restores_once_per_host_event_key(self) -> None:
        """This catches duplicate host lifecycle delivery advancing context twice."""

        self.activate()
        self.begin_gate()
        self.commit_gate()
        first = self.store.begin_context_epoch(
            session_id=SESSION_ID,
            source="compact",
            latest_observed_turn_id=TURN_ID,
            active_set_digest=ACTIVE_SET_DIGEST,
            compaction_key="compact-1",
        )
        replay = self.store.begin_context_epoch(
            session_id=SESSION_ID,
            source="compact",
            latest_observed_turn_id=TURN_ID,
            active_set_digest=ACTIVE_SET_DIGEST,
            compaction_key="compact-1",
        )

        self.assertEqual(first, replay)
        self.assertEqual(1, first.context_epoch)
        self.begin_gate(
            turn_id="turn-2", gate_id="gate-2", context_epoch=1, intent_epoch=1
        )
        self.commit_gate(
            turn_id="turn-2",
            gate_id="gate-2",
            result=_result(context_epoch=1, intent_epoch=1),
        )
        later = self.store.begin_context_epoch(
            session_id=SESSION_ID,
            source="clear",
            latest_observed_turn_id="turn-2",
            active_set_digest=ACTIVE_SET_DIGEST,
            compaction_key="clear-2",
        )

        self.assertEqual(2, later.context_epoch)
        self.assertEqual(2, self.store.get_session(SESSION_ID).context_epoch)

    def test_session_end_and_resume_preserve_authorization_but_require_revalidation(self) -> None:
        """This catches dormant Sessions losing authorization or resuming as active."""

        self.assertIsNone(self.store.mark_dormant(SESSION_ID, ended_at=NOW))
        activated = self.activate()
        dormant = self.store.mark_dormant(SESSION_ID, ended_at=NOW)
        resumed = self.store.begin_resume(
            SESSION_ID, cwd="/tmp/recall", now=NOW
        )

        self.assertEqual("active", activated.state)
        self.assertEqual("dormant", dormant.state)
        self.assertEqual("activating", resumed.state)
        self.assertEqual(TURN_ID, resumed.authorization_turn_id)
        with self.assertRaises(RecallGateConflict):
            self.begin_gate()

    def test_internal_threads_remain_recall_disabled(self) -> None:
        """This catches inherited capture context being treated as a recall activation."""

        bound = self.store.bind_internal_thread(
            thread_id="thread-capture",
            parent_thread_id=SESSION_ID,
            purpose="capture",
            operation_id="capture-1",
            now=NOW,
        )
        replay = self.store.bind_internal_thread(
            thread_id="thread-capture",
            parent_thread_id=SESSION_ID,
            purpose="capture",
            operation_id="capture-1",
            now=NOW,
        )

        self.assertEqual(bound, replay)
        self.assertTrue(self.store.is_internal_thread("thread-capture"))
        self.assertFalse(self.store.is_internal_thread(SESSION_ID))
        with self.assertRaises(RecallGateConflict):
            self.store.bind_activation(
                session_id="thread-capture",
                turn_id=TURN_ID,
                cwd="/tmp/recall",
                binding_id="activation-internal",
                now=NOW,
            )

    def test_open_does_not_change_capture_session_leases_table(self) -> None:
        """This catches recall schema changes leaking into Candidate lease state."""

        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute("CREATE TABLE session_leases (lease_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO session_leases VALUES ('lease-existing')")
        connection.commit()
        connection.close()
        self.store = RecallHostStore.open(self.path)

        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute("SELECT lease_id FROM session_leases").fetchall()
            columns = connection.execute("PRAGMA table_info(session_leases)").fetchall()
        finally:
            connection.close()
        self.assertEqual([("lease-existing",)], rows)
        self.assertEqual(["lease_id"], [column[1] for column in columns])

