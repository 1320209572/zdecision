"""Durable host-only state tests for trusted recall activation and gates."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
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

    def test_confirmation_attempt_commits_active_consent_only_after_enable(self) -> None:
        """This catches a rendered confirmation activating Recall before consent."""

        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "2" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )

        self.assertEqual("pending_confirmation", attempt.state)
        self.assertIsNone(self.store.get_session(SESSION_ID))
        self.assertEqual(
            "a" * 64,
            self.store.attach_activation_card(
                attempt.attempt_id, ui_digest="a" * 64
            ).ui_digest,
        )
        committed = self.store.decide_activation_attempt(
            attempt.attempt_id, action="enable", now=NOW
        )
        session = self.store.get_session(SESSION_ID)

        self.assertEqual("committed", committed.state)
        self.assertIsNotNone(session)
        self.assertEqual("active", session.state)
        self.assertEqual(0, session.intent_epoch)
        self.assertIsNone(session.active_intent_digest)
        self.assertIsNone(session.active_set_digest)

    def test_confirmation_attempt_freezes_card_and_terminal_choice(self) -> None:
        """This catches a retry replacing the card digest or the user's first choice."""

        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "4" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )
        self.assertEqual(
            attempt,
            self.store.create_activation_attempt(
                session_id=SESSION_ID,
                turn_id=TURN_ID,
                cwd="/tmp/recall",
                repository_id="repo_" + "1" * 32,
                repository_display_name="recall",
                attempt_id="activation_" + "4" * 32,
                now=NOW,
                expires_at=NOW + timedelta(minutes=15),
                plugin_root=None,
            ),
        )
        self.store.attach_activation_card(attempt.attempt_id, ui_digest="a" * 64)
        with self.assertRaises(RecallGateConflict):
            self.store.attach_activation_card(attempt.attempt_id, ui_digest="b" * 64)
        self.store.decide_activation_attempt(
            attempt.attempt_id, action="decline", now=NOW
        )
        with self.assertRaises(RecallGateConflict):
            self.store.decide_activation_attempt(
                attempt.attempt_id, action="enable", now=NOW
            )

    def test_confirmation_render_replay_returns_original_attempt_and_expiry(self) -> None:
        """This catches a clock tick or new generated ID denying a safe card replay."""

        created = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "6" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )

        replay = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "7" * 32,
            now=NOW + timedelta(seconds=30),
            expires_at=NOW + timedelta(minutes=15, seconds=30),
            plugin_root=None,
        )

        self.assertEqual(created.attempt_id, replay.attempt_id)
        self.assertEqual(created.expires_at, replay.expires_at)
        self.assertEqual("pending_confirmation", replay.state)
        self.assertIsNone(self.store.get_session(SESSION_ID))

    def test_confirmation_attempt_rejects_changed_trusted_coordinates(self) -> None:
        """This catches a retry reusing a Turn for another repository or CWD."""

        created = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "8" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )
        for cwd, repository_id in (
            ("/tmp/other", "repo_" + "1" * 32),
            ("/tmp/recall", "repo_" + "2" * 32),
        ):
            with self.subTest(cwd=cwd, repository_id=repository_id):
                with self.assertRaises(RecallGateConflict):
                    self.store.create_activation_attempt(
                        session_id=SESSION_ID,
                        turn_id=TURN_ID,
                        cwd=cwd,
                        repository_id=repository_id,
                        repository_display_name="recall",
                        attempt_id="activation_" + "9" * 32,
                        now=NOW + timedelta(seconds=1),
                        expires_at=NOW + timedelta(minutes=15, seconds=1),
                        plugin_root=None,
                    )
                frozen = self.store.get_activation_attempt(created.attempt_id)
                self.assertEqual("/tmp/recall", frozen.cwd)
                self.assertEqual("repo_" + "1" * 32, frozen.repository_id)
                self.assertIsNone(self.store.get_session(SESSION_ID))

    def test_confirmation_attempt_rejects_changed_trusted_bundle(self) -> None:
        """This catches a retry replacing the Hook-verified plugin bundle."""

        def plugin_root(name: str) -> Path:
            root = Path(self.temporary_directory.name) / name
            (root / ".codex-plugin").mkdir(parents=True)
            (root / "skills/decision-recall").mkdir(parents=True)
            (root / ".codex-plugin/plugin.json").write_text(
                json.dumps(
                    {
                        "name": "zdecision",
                        "skills": "./skills/",
                        "mcpServers": "./.mcp.json",
                    }
                ),
                "utf-8",
            )
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "zdecision-local": {
                                "command": "zdecision-agent",
                                "args": ["mcp"],
                            }
                        }
                    }
                ),
                "utf-8",
            )
            (root / "skills/decision-recall/SKILL.md").write_text(name, "utf-8")
            return root

        original_root = plugin_root("plugin-a")
        changed_root = plugin_root("plugin-b")
        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "d" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=str(original_root),
        )

        with self.assertRaises(RecallGateConflict):
            self.store.create_activation_attempt(
                session_id=SESSION_ID,
                turn_id=TURN_ID,
                cwd="/tmp/recall",
                repository_id="repo_" + "1" * 32,
                repository_display_name="recall",
                attempt_id="activation_" + "e" * 32,
                now=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(minutes=15, seconds=1),
                plugin_root=str(changed_root),
            )

        frozen = self.store.get_activation_attempt(attempt.attempt_id)
        self.assertEqual(str(original_root.resolve()), frozen.plugin_root)
        self.assertIsNone(self.store.get_session(SESSION_ID))

    def test_expired_confirmation_fails_without_creating_consent(self) -> None:
        """This catches a timed-out enable creating an active Session."""

        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "a" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )
        self.store.attach_activation_card(attempt.attempt_id, ui_digest="a" * 64)

        with self.assertRaises(RecallGateConflict):
            self.store.decide_activation_attempt(
                attempt.attempt_id, action="enable", now=NOW + timedelta(minutes=15)
            )

        self.assertEqual(
            "failed", self.store.get_activation_attempt(attempt.attempt_id).state
        )
        self.assertIsNone(self.store.get_session(SESSION_ID))

    def test_confirmation_attempt_survives_store_reopen(self) -> None:
        """This catches a restart losing a pending card or its frozen digest."""

        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "b" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )
        self.store.attach_activation_card(attempt.attempt_id, ui_digest="a" * 64)
        self.store.close()
        self.store = RecallHostStore.open(self.path)

        recovered = self.store.get_activation_attempt(attempt.attempt_id)

        self.assertEqual("pending_confirmation", recovered.state)
        self.assertEqual("a" * 64, recovered.ui_digest)
        self.assertIsNone(self.store.get_session(SESSION_ID))

    def test_session_end_retires_pending_confirmation_without_consent(self) -> None:
        """This catches a closed Session retaining an actionable confirmation card."""

        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id="repo_" + "1" * 32,
            repository_display_name="recall",
            attempt_id="activation_" + "5" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )

        retired = self.store.retire_activation_attempts(SESSION_ID, now=NOW)

        self.assertEqual("cancelled", retired[0].state)
        self.assertIsNone(self.store.get_session(SESSION_ID))
        with self.assertRaises(RecallGateConflict):
            self.store.decide_activation_attempt(
                attempt.attempt_id, action="enable", now=NOW
            )

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

    def test_committed_gate_records_per_turn_reference_state_version(self) -> None:
        """This catches treating a new empty reference set as legacy unknown state."""

        self.activate()
        self.begin_gate()
        committed = self.commit_gate(active_set_digest=None)

        self.assertEqual(None, committed.active_set_digest)
        self.assertEqual(1, committed.reference_state_version)

    def test_pre_migration_committed_gate_keeps_unknown_reference_state(self) -> None:
        """This catches migration backfilling a legacy gate from current Session state."""

        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE recall_turn_gates")
            connection.execute(
                """
                CREATE TABLE recall_turn_gates (
                    gate_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL, context_epoch INTEGER NOT NULL,
                    intent_epoch INTEGER NOT NULL, active_generation INTEGER,
                    state TEXT NOT NULL, result_digest TEXT, commit_fingerprint TEXT,
                    plugin_root TEXT, plugin_bundle_digest TEXT,
                    UNIQUE(session_id, turn_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO recall_turn_gates VALUES (?, ?, ?, 0, 1, NULL,
                    'committed', ?, ?, NULL, NULL)
                """,
                (GATE_ID, SESSION_ID, TURN_ID, "a" * 64, "b" * 64),
            )
            connection.commit()
        self.store = RecallHostStore.open(self.path)

        legacy = self.store.require_committed_gate(SESSION_ID, TURN_ID)

        self.assertIsNone(legacy.active_set_digest)
        self.assertIsNone(legacy.reference_state_version)

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

    def test_cross_bound_gate_id_does_not_block_another_pending_turn(self) -> None:
        """This catches a mismatched ID writing to a different trusted gate."""

        self.activate()
        self.activate(
            session_id="session-2", turn_id="turn-2", binding_id="activation-2"
        )
        self.begin_gate()
        self.begin_gate(session_id="session-2", turn_id="turn-2", gate_id="gate-2")

        with self.assertRaises(RecallGateConflict):
            self.commit_gate(gate_id="gate-2")

        self.assertEqual("pending", self.begin_gate().state)
        self.assertEqual(
            "pending",
            self.begin_gate(
                session_id="session-2", turn_id="turn-2", gate_id="gate-2"
            ).state,
        )

    def test_invalid_result_payload_blocks_the_exact_pending_gate(self) -> None:
        """This catches payload validation rolling back instead of blocking a gate."""

        self.activate()
        self.begin_gate()

        with self.assertRaises(RecallGateConflict):
            self.commit_gate(result=_result(intent_digest=""))

        self.assertEqual("blocked", self.begin_gate().state)

    def test_invalid_active_set_blocks_the_exact_pending_gate(self) -> None:
        """This catches pre-transaction active-set validation leaving a gate pending."""

        self.activate()
        self.begin_gate()

        with self.assertRaises(RecallGateConflict):
            self.commit_gate(active_set_digest="")

        self.assertEqual("blocked", self.begin_gate().state)

    def test_terminal_gate_replay_requires_the_full_submission_fingerprint(self) -> None:
        """This catches terminal replays ignoring the active-set identity."""

        self.activate()
        self.begin_gate()
        committed = self.commit_gate()

        self.assertEqual(committed, self.commit_gate())
        with self.assertRaises(RecallGateConflict):
            self.commit_gate(active_set_digest="set-other")

        self.begin_gate(
            turn_id="turn-2", gate_id="gate-2", intent_epoch=1
        )
        blocked_result = _result(
            disposition="blocked", context_epoch=0, intent_epoch=1
        )
        blocked = self.commit_gate(
            turn_id="turn-2", gate_id="gate-2", result=blocked_result
        )

        self.assertEqual("blocked", blocked.state)
        self.assertEqual(
            blocked,
            self.commit_gate(
                turn_id="turn-2", gate_id="gate-2", result=blocked_result
            ),
        )
        with self.assertRaises(RecallGateConflict):
            self.commit_gate(
                turn_id="turn-2",
                gate_id="gate-2",
                result=blocked_result,
                active_set_digest="set-other",
            )

    def test_late_internal_binding_terminates_pending_thread_recall(self) -> None:
        """This catches a late Capture binding committing an inherited pending gate."""

        self.activate(session_id="thread-capture", binding_id="activation-thread")
        self.begin_gate(session_id="thread-capture", gate_id="gate-thread")
        self.store.bind_internal_thread(
            thread_id="thread-capture",
            parent_thread_id=SESSION_ID,
            purpose="capture",
            operation_id="capture-late",
            now=NOW,
        )

        with self.assertRaises(RecallGateConflict):
            self.commit_gate(session_id="thread-capture", gate_id="gate-thread")
        self.assertIsNone(self.store.get_session("thread-capture"))

    def test_late_internal_binding_denies_committed_receipts_and_restoration(self) -> None:
        """This catches late reconciliation binding retaining prior recall authority."""

        self.activate(session_id="thread-reconciliation", binding_id="activation-thread")
        self.begin_gate(session_id="thread-reconciliation", gate_id="gate-thread")
        self.commit_gate(session_id="thread-reconciliation", gate_id="gate-thread")
        self.store.begin_context_epoch(
            session_id="thread-reconciliation",
            source="compact",
            latest_observed_turn_id=TURN_ID,
            active_set_digest=ACTIVE_SET_DIGEST,
            compaction_key="compact-thread",
        )
        self.store.bind_internal_thread(
            thread_id="thread-reconciliation",
            parent_thread_id=SESSION_ID,
            purpose="reconciliation",
            operation_id="reconciliation-late",
            now=NOW,
        )

        with self.assertRaises(RecallGateConflict):
            self.store.require_committed_gate("thread-reconciliation", TURN_ID)
        with self.assertRaises(RecallGateConflict):
            self.store.begin_context_epoch(
                session_id="thread-reconciliation",
                source="compact",
                latest_observed_turn_id=TURN_ID,
                active_set_digest=ACTIVE_SET_DIGEST,
                compaction_key="compact-thread",
            )

    def test_revalidation_reactivates_without_replacing_original_authorization(self) -> None:
        """This catches trusted resume revalidation overwriting original authorization."""

        self.activate()
        self.store.mark_dormant(SESSION_ID, ended_at=NOW)
        self.store.begin_resume(SESSION_ID, cwd="/tmp/recall", now=NOW)
        revalidated = self.activate(
            turn_id="turn-revalidate", binding_id="activation-revalidate"
        )

        self.assertEqual("active", revalidated.state)
        self.assertEqual(TURN_ID, revalidated.authorization_turn_id)

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

    def test_context_epoch_atomically_rebases_only_the_exact_pending_gate(self) -> None:
        """This catches compact advancing past a frozen old-epoch pending gate."""

        self.activate()
        self.begin_gate()
        self.commit_gate()
        self.begin_gate(
            turn_id="turn-2", gate_id="gate-2", intent_epoch=1
        )
        self.begin_gate(
            turn_id="turn-3", gate_id="gate-3", intent_epoch=1
        )

        with self.assertRaises(RecallGateConflict):
            self.store.begin_context_epoch(
                session_id=SESSION_ID,
                source="compact",
                latest_observed_turn_id=TURN_ID,
                active_set_digest=ACTIVE_SET_DIGEST,
                compaction_key="compact-rebase",
                pending_turn_id="turn-2",
                pending_gate_id="gate-3",
                rebased_gate_id="gate-2-context-1",
            )

        self.assertEqual(0, self.store.get_session(SESSION_ID).context_epoch)
        self.assertEqual(
            "gate-2", self.store.get_turn_gate(SESSION_ID, "turn-2").gate_id
        )
        restoration = self.store.begin_context_epoch(
            session_id=SESSION_ID,
            source="compact",
            latest_observed_turn_id=TURN_ID,
            active_set_digest=ACTIVE_SET_DIGEST,
            compaction_key="compact-rebase",
            pending_turn_id="turn-2",
            pending_gate_id="gate-2",
            rebased_gate_id="gate-2-context-1",
        )

        rebased = self.store.get_turn_gate(SESSION_ID, "turn-2")
        untouched = self.store.get_turn_gate(SESSION_ID, "turn-3")
        self.assertEqual(1, restoration.context_epoch)
        self.assertEqual("gate-2-context-1", rebased.gate_id)
        self.assertEqual(1, rebased.context_epoch)
        self.assertEqual("pending", rebased.state)
        self.assertEqual("gate-3", untouched.gate_id)
        self.assertEqual(0, untouched.context_epoch)

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
