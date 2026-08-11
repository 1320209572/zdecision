"""Durable host-only state tests for trusted recall activation and gates."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import inspect
from pathlib import Path

from zdecision.agent.recall_host_state import (
    RecallGateConflict,
    RecallHostStore,
)
from zdecision.agent.recall_plugin_identity import RecallPluginIdentity
from tests.test_recall_handoff_contracts import (
    formal_decision,
    ready_preflight,
    valid_intent,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.handoff import (
    RecallApplicationSubmission,
    RecallShortlist,
    RecalledDecision,
    build_handoff_context,
)
from zdecision.recall.session import RecallIntent, TurnGateResult


NOW = datetime(2026, 8, 6, 3, 0, tzinfo=UTC)
SESSION_ID = "session-1"
TURN_ID = "turn-1"
ACTIVATION_ID = "activation-1"
GATE_ID = "gate-1"
ACTIVE_SET_DIGEST = "set-a"
ATTEMPT_ID = "activation_" + "f" * 32
DELIVERY_ID = "delivery_" + "d" * 32


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
    )


class RecallHostStoreTests(unittest.TestCase):
    def test_reopened_other_identity_cannot_begin_a_frozen_delivery(self) -> None:
        """This catches durable consent being authorized by a different identity."""

        root = Path(__file__).resolve().parents[1] / "plugins/zdecision"
        intent = valid_intent()
        preflight = ready_preflight(intent=intent)
        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id=preflight.repository_id,
            repository_display_name=preflight.repository_display_name,
            attempt_id=ATTEMPT_ID,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=str(root),
            intent=intent,
            preflight=preflight,
        )
        self.store.attach_activation_card(attempt.attempt_id, ui_digest="a" * 64)
        self.store.close()
        other = RecallPluginIdentity(
            plugin_name="disposable",
            mcp_server_key="disposable-local",
            mcp_command="python",
            mcp_args=("launcher.py", "mcp"),
            hook_command="python launcher.py hook",
            recall_skill_relative_path="skills/disposable/SKILL.md",
        )
        self.store = RecallHostStore.open(self.path, identity=other)
        self.addCleanup(self.store.close)
        with self.assertRaises(RecallGateConflict):
            self.store.begin_delivery(
                attempt_id=attempt.attempt_id,
                delivery_id=DELIVERY_ID,
                claim_token="claim_" + "c" * 32,
                current_ui_digest="a" * 64,
                now=NOW,
                claim_expires_at=NOW + timedelta(seconds=30),
            )
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

    def handoff_values(self, *, two_items: bool = False):
        intent = valid_intent()
        preflight = ready_preflight(intent=intent)
        first = RecalledDecision.create(
            decision_space_id="space-product",
            revision=formal_decision(),
            match_reason="Exact product match",
        )
        items = (first,)
        if two_items:
            second_revision = replace(
                formal_decision(claim="The second handoff item remains complete."),
                decision_id="dec_" + "9" * 32,
            )
            second = RecalledDecision.create(
                decision_space_id="space-product",
                revision=second_revision,
                match_reason="Exact capability match",
            )
            items += (second,)
        shortlist = RecallShortlist.create(preflight=preflight, items=items)
        return intent, preflight, shortlist

    def create_handoff_attempt(self, *, two_items: bool = False):
        intent, preflight, shortlist = self.handoff_values(two_items=two_items)
        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id=preflight.repository_id,
            repository_display_name=preflight.repository_display_name,
            attempt_id=ATTEMPT_ID,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
            intent=intent,
            preflight=preflight,
        )
        self.store.attach_activation_card(attempt.attempt_id, ui_digest="a" * 64)
        return attempt, preflight, shortlist

    def prepare_handoff(self, *, two_items: bool = False):
        attempt, preflight, shortlist = self.create_handoff_attempt(
            two_items=two_items
        )
        claim = self.store.begin_delivery(
            attempt_id=attempt.attempt_id,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "c" * 32,
            current_ui_digest="a" * 64,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        context_text = build_handoff_context(DELIVERY_ID, preflight, shortlist)
        delivery = self.store.commit_prepared_delivery(
            delivery_id=DELIVERY_ID,
            claim_token=claim.claim_token,
            shortlist=shortlist,
            context_text=context_text,
            now=NOW,
        )
        return attempt, preflight, shortlist, context_text, delivery

    def acknowledge_handoff(self, *, two_items: bool = False):
        attempt, preflight, shortlist, context_text, delivery = self.prepare_handoff(
            two_items=two_items
        )
        acknowledged = self.store.ack_delivery(
            delivery_id=DELIVERY_ID,
            context_digest=delivery.context_digest,
            now=NOW,
        )
        return attempt, preflight, shortlist, context_text, acknowledged

    @staticmethod
    def application(shortlist, dispositions, *, reason: str = "Bounded reason"):
        return RecallApplicationSubmission.from_dict(
            {
                "delivery_id": DELIVERY_ID,
                "items": [
                    {
                        "decision_id": item.revision.decision_id,
                        "revision": item.revision.revision,
                        "digest": item.digest,
                        "disposition": disposition,
                        "reason": reason,
                    }
                    for item, disposition in zip(shortlist.items, dispositions)
                ],
            }
        )

    def test_unselected_session_has_no_state_row(self) -> None:
        """This catches creating recall state before a trusted activation."""

        self.assertIsNone(self.store.get_session(SESSION_ID))

    def test_begin_delivery_atomically_accepts_consent_and_creates_activating_session(
        self,
    ) -> None:
        """This catches consent committing without its preparing delivery."""

        self.assertTrue(
            hasattr(self.store, "begin_delivery"),
            "RecallHostStore.begin_delivery is required",
        )
        intent = valid_intent()
        preflight = ready_preflight(intent=intent)
        attempt = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id=preflight.repository_id,
            repository_display_name=preflight.repository_display_name,
            attempt_id=ATTEMPT_ID,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
            intent=intent,
            preflight=preflight,
        )
        self.store.attach_activation_card(attempt.attempt_id, ui_digest="a" * 64)

        claim = self.store.begin_delivery(
            attempt_id=attempt.attempt_id,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "c" * 32,
            current_ui_digest="a" * 64,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )

        self.assertTrue(claim.owned)
        self.assertEqual("preparing", claim.delivery.state)
        self.assertEqual("committed", self.store.get_activation_attempt(ATTEMPT_ID).state)
        session = self.store.get_session(SESSION_ID)
        self.assertEqual("activating", session.state)
        self.assertEqual("recall-handoff-v1", session.protocol_version)
        self.assertEqual(preflight.repository_id, session.repository_id)

    def test_delivery_claim_has_one_owner_and_expired_preparing_claim_is_replaced(
        self,
    ) -> None:
        """This catches concurrent retrieval owners or a permanently stranded claim."""

        attempt, _, _ = self.create_handoff_attempt()
        first = self.store.begin_delivery(
            attempt_id=attempt.attempt_id,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "1" * 32,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        competitor_store = RecallHostStore.open(self.path)
        self.addCleanup(competitor_store.close)

        competitor = competitor_store.begin_delivery(
            attempt_id=attempt.attempt_id,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "2" * 32,
            now=NOW + timedelta(seconds=10),
            claim_expires_at=NOW + timedelta(seconds=40),
        )
        takeover = competitor_store.begin_delivery(
            attempt_id=attempt.attempt_id,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "3" * 32,
            now=NOW + timedelta(seconds=30),
            claim_expires_at=NOW + timedelta(seconds=60),
        )

        self.assertTrue(first.owned)
        self.assertFalse(competitor.owned)
        self.assertIsNone(competitor.claim_token)
        self.assertTrue(takeover.owned)
        self.assertEqual("claim_" + "3" * 32, takeover.claim_token)
        self.assertEqual(DELIVERY_ID, takeover.delivery.delivery_id)

    def test_begin_delivery_rolls_back_consent_and_session_when_insert_fails(
        self,
    ) -> None:
        """This catches consent surviving a failed delivery insert."""

        attempt, _, _ = self.create_handoff_attempt()
        self.store._connection.execute(  # noqa: SLF001 - transaction fault injection
            """
            CREATE TRIGGER fail_recall_delivery_insert
            BEFORE INSERT ON recall_deliveries
            BEGIN
                SELECT RAISE(ABORT, 'forced delivery rollback');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.begin_delivery(
                attempt_id=attempt.attempt_id,
                delivery_id=DELIVERY_ID,
                claim_token="claim_" + "4" * 32,
                now=NOW,
                claim_expires_at=NOW + timedelta(seconds=30),
            )

        self.assertEqual(
            "pending_confirmation",
            self.store.get_activation_attempt(attempt.attempt_id).state,
        )
        self.assertIsNone(self.store.get_session(SESSION_ID))
        self.assertIsNone(self.store.delivery_for_attempt(attempt.attempt_id))

    def test_prepared_delivery_is_canonical_immutable_and_reopens(self) -> None:
        """This catches prepared handoff bytes changing across retry or restart."""

        self.assertTrue(
            hasattr(self.store, "commit_prepared_delivery"),
            "RecallHostStore.commit_prepared_delivery is required",
        )
        attempt, preflight, shortlist = self.create_handoff_attempt()
        claim = self.store.begin_delivery(
            attempt_id=attempt.attempt_id,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "5" * 32,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        context_text = build_handoff_context(DELIVERY_ID, preflight, shortlist)

        prepared = self.store.commit_prepared_delivery(
            delivery_id=DELIVERY_ID,
            claim_token=claim.claim_token,
            shortlist=shortlist,
            context_text=context_text,
            now=NOW,
        )

        self.assertEqual("delivery_claimed", prepared.state)
        self.assertEqual(shortlist, prepared.shortlist)
        self.assertEqual(
            hashlib.sha256(canonical_json_bytes(shortlist.to_dict())).hexdigest(),
            prepared.snapshot_digest,
        )
        self.assertEqual(
            hashlib.sha256(context_text.encode("utf-8")).hexdigest(),
            prepared.context_digest,
        )
        with closing(sqlite3.connect(self.path)) as connection:
            preflight_json = connection.execute(
                "SELECT preflight_json FROM recall_activation_attempts WHERE attempt_id = ?",
                (ATTEMPT_ID,),
            ).fetchone()[0]
            shortlist_json = connection.execute(
                "SELECT shortlist_json FROM recall_deliveries WHERE delivery_id = ?",
                (DELIVERY_ID,),
            ).fetchone()[0]
        self.assertEqual(
            canonical_json_bytes(preflight.to_dict()).decode("utf-8"), preflight_json
        )
        self.assertEqual(
            canonical_json_bytes(shortlist.to_dict()).decode("utf-8"), shortlist_json
        )
        self.assertEqual(
            prepared,
            self.store.commit_prepared_delivery(
                delivery_id=DELIVERY_ID,
                claim_token=claim.claim_token,
                shortlist=shortlist,
                context_text=context_text,
                now=NOW,
            ),
        )
        with self.assertRaises(RecallGateConflict):
            self.store.commit_prepared_delivery(
                delivery_id=DELIVERY_ID,
                claim_token=claim.claim_token,
                shortlist=shortlist,
                context_text=context_text + " ",
                now=NOW,
            )

        self.store.close()
        reopened = RecallHostStore.open(self.path)
        self.store = reopened
        self.addCleanup(reopened.close)
        self.assertEqual(prepared, reopened.get_delivery(DELIVERY_ID))
        self.assertEqual(prepared, reopened.delivery_for_attempt(ATTEMPT_ID))

    def test_store_delivery_api_contains_no_provider_work(self) -> None:
        """This catches retrieval dependencies leaking into SQLite transactions."""

        for method_name in ("begin_delivery", "commit_prepared_delivery"):
            self.assertTrue(hasattr(RecallHostStore, method_name))
            parameters = inspect.signature(getattr(RecallHostStore, method_name)).parameters
            self.assertNotIn("provider", parameters)

    def test_delivery_ack_requires_the_exact_context_digest(self) -> None:
        """This catches acknowledging bytes other than the frozen handoff."""

        self.assertTrue(
            hasattr(self.store, "ack_delivery"),
            "RecallHostStore.ack_delivery is required",
        )
        _, _, _, _, prepared = self.prepare_handoff()

        with self.assertRaises(RecallGateConflict):
            self.store.ack_delivery(
                delivery_id=DELIVERY_ID,
                context_digest="0" * 64,
                now=NOW,
            )

        self.assertEqual(
            "delivery_claimed", self.store.get_delivery(DELIVERY_ID).state
        )
        acknowledged = self.store.ack_delivery(
            delivery_id=DELIVERY_ID,
            context_digest=prepared.context_digest,
            now=NOW,
        )
        self.assertEqual("host_delivered", acknowledged.state)
        self.assertEqual(
            acknowledged,
            self.store.ack_delivery(
                delivery_id=DELIVERY_ID,
                context_digest=prepared.context_digest,
                now=NOW,
            ),
        )

    def test_eligible_delivery_for_session_returns_only_one_v1_delivered_handoff(
        self,
    ) -> None:
        """This catches Hook lookup accepting absent or non-delivered handoffs."""

        self.assertIsNone(self.store.eligible_delivery_for_session(SESSION_ID))
        _, _, _, _, acknowledged = self.acknowledge_handoff()

        self.assertEqual(
            acknowledged,
            self.store.eligible_delivery_for_session(SESSION_ID),
        )
        with self.store._connection:  # noqa: SLF001 - fail-closed state fixture
            self.store._connection.execute(
                "UPDATE recall_deliveries SET state = 'blocked' WHERE delivery_id = ?",
                (DELIVERY_ID,),
            )

        self.assertIsNone(self.store.eligible_delivery_for_session(SESSION_ID))

    def test_eligible_delivery_lookup_accepts_unknown_but_rejects_legacy_and_internal(
        self,
    ) -> None:
        """This catches uncertain, legacy, or internal delivery authority confusion."""

        _, _, _, _, prepared = self.prepare_handoff()
        unknown = self.store.mark_delivery_unknown(
            delivery_id=DELIVERY_ID,
            now=NOW + timedelta(seconds=30),
        )
        self.assertEqual(
            unknown,
            self.store.eligible_delivery_for_session(SESSION_ID),
        )

        with self.store._connection:  # noqa: SLF001 - legacy boundary fixture
            self.store._connection.execute(
                """
                UPDATE recall_activation_attempts SET protocol_version = NULL
                WHERE attempt_id = ?
                """,
                (prepared.attempt_id,),
            )
        self.assertIsNone(self.store.eligible_delivery_for_session(SESSION_ID))

        with self.store._connection:  # noqa: SLF001 - restore exact v1 fixture
            self.store._connection.execute(
                """
                UPDATE recall_activation_attempts SET protocol_version = ?
                WHERE attempt_id = ?
                """,
                ("recall-handoff-v1", prepared.attempt_id),
            )
        self.store.bind_internal_thread(
            thread_id=SESSION_ID,
            parent_thread_id="parent-session",
            purpose="capture",
            operation_id="lookup-internal",
            now=NOW,
        )
        self.assertIsNone(self.store.eligible_delivery_for_session(SESSION_ID))

    def test_eligible_delivery_lookup_rejects_multiple_candidates(self) -> None:
        """This catches an ambiguous delivered handoff being selected by row order."""

        _, _, _, _, acknowledged = self.acknowledge_handoff()
        second_attempt = "activation_" + "7" * 32
        second_delivery = "delivery_" + "7" * 32
        with self.store._connection:  # noqa: SLF001 - ambiguous-row fixture
            self.store._connection.execute(
                """
                INSERT INTO recall_activation_attempts(
                    attempt_id, session_id, turn_id, cwd, repository_id,
                    repository_display_name, state, created_at, expires_at,
                    plugin_root, plugin_bundle_digest, ui_digest, result_digest,
                    protocol_version, preflight_json, preflight_digest
                )
                SELECT ?, session_id, 'turn-duplicate', cwd, repository_id,
                       repository_display_name, state, created_at, expires_at,
                       plugin_root, plugin_bundle_digest, ui_digest, result_digest,
                       protocol_version, preflight_json, preflight_digest
                FROM recall_activation_attempts WHERE attempt_id = ?
                """,
                (second_attempt, acknowledged.attempt_id),
            )
            self.store._connection.execute(
                """
                INSERT INTO recall_deliveries(
                    delivery_id, attempt_id, session_id, state,
                    preflight_digest, claim_token, claim_expires_at,
                    shortlist_json, snapshot_digest, context_text,
                    context_digest, application_json, application_digest,
                    application_receipt_id, created_at, updated_at
                )
                SELECT ?, ?, session_id, state, preflight_digest, claim_token,
                       claim_expires_at, shortlist_json, snapshot_digest,
                       context_text, context_digest, application_json,
                       application_digest, application_receipt_id, created_at,
                       updated_at
                FROM recall_deliveries WHERE delivery_id = ?
                """,
                (second_delivery, second_attempt, DELIVERY_ID),
            )

        self.assertIsNone(self.store.eligible_delivery_for_session(SESSION_ID))

    def test_unknown_delivery_is_explicitly_reclaimed_without_changing_bytes(
        self,
    ) -> None:
        """This catches an automatic resend or retry with substituted context."""

        self.assertTrue(hasattr(self.store, "mark_delivery_unknown"))
        self.assertTrue(hasattr(self.store, "claim_delivery_retry"))
        _, _, _, _, prepared = self.prepare_handoff()
        frozen = (
            prepared.preflight,
            prepared.shortlist,
            prepared.snapshot_digest,
            prepared.context_text,
            prepared.context_digest,
        )

        with self.assertRaises(RecallGateConflict):
            self.store.mark_delivery_unknown(
                delivery_id=DELIVERY_ID,
                now=NOW + timedelta(seconds=29),
            )
        unknown = self.store.mark_delivery_unknown(
            delivery_id=DELIVERY_ID,
            now=NOW + timedelta(seconds=30),
        )
        retry = self.store.claim_delivery_retry(
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "6" * 32,
            now=NOW + timedelta(seconds=30),
            claim_expires_at=NOW + timedelta(seconds=60),
        )

        self.assertEqual("delivery_unknown", unknown.state)
        self.assertTrue(retry.owned)
        self.assertEqual("delivery_claimed", retry.delivery.state)
        self.assertEqual(frozen, (
            retry.delivery.preflight,
            retry.delivery.shortlist,
            retry.delivery.snapshot_digest,
            retry.delivery.context_text,
            retry.delivery.context_digest,
        ))
        competitor_store = RecallHostStore.open(self.path)
        self.addCleanup(competitor_store.close)
        competitor = competitor_store.claim_delivery_retry(
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "7" * 32,
            now=NOW + timedelta(seconds=40),
            claim_expires_at=NOW + timedelta(seconds=70),
        )
        self.assertFalse(competitor.owned)
        self.assertIsNone(competitor.claim_token)

    def test_application_requires_every_frozen_item_and_commits_only_applicable(
        self,
    ) -> None:
        """This catches partial classification or false positives becoming active."""

        self.assertTrue(hasattr(self.store, "commit_delivery_application"))
        self.assertTrue(hasattr(self.store, "list_active_items"))
        _, preflight, shortlist, _, acknowledged = self.acknowledge_handoff(
            two_items=True
        )
        gate = self.store.begin_turn_gate(
            session_id=SESSION_ID,
            turn_id="turn-next",
            context_epoch=0,
            intent_epoch=0,
            active_generation=preflight.generation,
            gate_id="gate-next",
        )
        incomplete = self.application(shortlist, ("applicable",))
        with self.assertRaises(RecallGateConflict):
            self.store.commit_delivery_application(
                session_id=SESSION_ID,
                turn_id="turn-next",
                gate_id=gate.gate_id,
                delivery_id=DELIVERY_ID,
                submission=incomplete,
                now=NOW,
            )
        self.assertEqual(
            "host_delivered", self.store.get_delivery(DELIVERY_ID).state
        )
        self.assertEqual(
            "pending", self.store.get_turn_gate(SESSION_ID, "turn-next").state
        )

        submission = self.application(
            shortlist, ("applicable", "not_applicable")
        )
        committed = self.store.commit_delivery_application(
            session_id=SESSION_ID,
            turn_id="turn-next",
            gate_id=gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=submission,
            now=NOW,
        )
        active = self.store.list_active_items(SESSION_ID)
        session = self.store.get_session(SESSION_ID)

        self.assertEqual("application_committed", committed.state)
        self.assertEqual(submission, committed.application)
        self.assertIsNotNone(committed.application_receipt_id)
        self.assertEqual("committed", self.store.get_turn_gate(SESSION_ID, "turn-next").state)
        self.assertEqual("active", session.state)
        self.assertEqual(1, session.intent_epoch)
        self.assertEqual(preflight.intent.digest, session.active_intent_digest)
        self.assertEqual(preflight.intent, session.active_intent)
        self.assertEqual(DELIVERY_ID, session.active_delivery_id)
        self.assertEqual(
            committed.application_receipt_id, session.application_receipt_id
        )
        self.assertIsNotNone(session.active_set_digest)
        self.assertEqual("turn-next", session.last_gate_turn_id)
        self.assertEqual(1, len(active))
        self.assertEqual(shortlist.items[0].digest, active[0].digest)
        self.assertEqual(shortlist.items[0], active[0].envelope)
        self.assertEqual(committed.application_receipt_id, active[0].application_receipt_id)
        with closing(sqlite3.connect(self.path)) as connection:
            stored = connection.execute(
                "SELECT application_json FROM recall_deliveries WHERE delivery_id = ?",
                (DELIVERY_ID,),
            ).fetchone()[0]
        self.assertEqual(
            canonical_json_bytes(submission.to_dict()).decode("utf-8"), stored
        )

    def test_all_not_applicable_commits_an_active_empty_set(self) -> None:
        """This catches a valid empty active epoch remaining activating forever."""

        self.assertTrue(hasattr(self.store, "commit_delivery_application"))
        _, preflight, shortlist, _, _ = self.acknowledge_handoff()
        gate = self.store.begin_turn_gate(
            session_id=SESSION_ID,
            turn_id="turn-next",
            context_epoch=0,
            intent_epoch=0,
            active_generation=preflight.generation,
            gate_id="gate-next",
        )
        submission = self.application(shortlist, ("not_applicable",))

        committed = self.store.commit_delivery_application(
            session_id=SESSION_ID,
            turn_id="turn-next",
            gate_id=gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=submission,
            now=NOW,
        )

        self.assertEqual("application_committed", committed.state)
        self.assertEqual((), self.store.list_active_items(SESSION_ID))
        self.assertEqual("active", self.store.get_session(SESSION_ID).state)
        self.assertEqual(
            hashlib.sha256(canonical_json_bytes([])).hexdigest(),
            self.store.get_session(SESSION_ID).active_set_digest,
        )

    def test_changed_intent_delivery_is_frozen_for_an_already_consented_session(
        self,
    ) -> None:
        """This catches changed-intent retrieval escaping durable gate ownership."""

        _, preflight, shortlist, _, _ = self.acknowledge_handoff()
        initial_gate = self.store.begin_turn_gate(
            session_id=SESSION_ID,
            turn_id="turn-apply",
            context_epoch=0,
            intent_epoch=0,
            active_generation=preflight.generation,
            gate_id="gate-apply",
        )
        self.store.commit_delivery_application(
            session_id=SESSION_ID,
            turn_id="turn-apply",
            gate_id=initial_gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=self.application(shortlist, ("applicable",)),
            now=NOW,
        )
        changed_intent = RecallIntent.from_dict(
            {**preflight.intent.to_dict(), "feature_goal": "Changed feature"}
        )
        changed_preflight = replace(preflight, intent=changed_intent, generation=8)
        changed_item = RecalledDecision.create(
            decision_space_id=changed_preflight.target_decision_space_ids[0],
            revision=replace(
                formal_decision(claim="The changed feature uses this decision."),
                decision_id="dec_" + "8" * 32,
            ),
            match_reason="Changed feature match",
        )
        changed_shortlist = RecallShortlist.create(
            preflight=changed_preflight,
            items=(changed_item,),
        )
        changed_gate = self.store.begin_turn_gate(
            session_id=SESSION_ID,
            turn_id="turn-changed",
            context_epoch=0,
            intent_epoch=1,
            active_generation=None,
            gate_id="gate-changed",
        )
        changed_delivery_id = "delivery_" + "8" * 32

        claim = self.store.begin_intent_delivery(
            session_id=SESSION_ID,
            turn_id="turn-changed",
            gate_id=changed_gate.gate_id,
            attempt_id="intent_attempt_" + "8" * 32,
            delivery_id=changed_delivery_id,
            claim_token="claim_" + "8" * 32,
            preflight=changed_preflight,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
            retire_active_set=False,
        )

        self.assertTrue(claim.owned)
        self.assertEqual("preparing", claim.delivery.state)
        self.assertEqual("activating", self.store.get_session(SESSION_ID).state)
        self.assertEqual(1, len(self.store.list_active_items(SESSION_ID)))
        context = build_handoff_context(
            changed_delivery_id, changed_preflight, changed_shortlist
        )
        delivered = self.store.commit_intent_delivery(
            delivery_id=changed_delivery_id,
            claim_token=claim.claim_token,
            shortlist=changed_shortlist,
            context_text=context,
            now=NOW,
        )

        self.assertEqual("host_delivered", delivered.state)
        self.assertEqual(
            "pending",
            self.store.get_turn_gate(SESSION_ID, "turn-changed").state,
        )
        reopened = RecallHostStore.open(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(delivered, reopened.get_delivery(changed_delivery_id))

    def test_application_rolls_back_delivery_gate_session_items_and_receipt(self) -> None:
        """This catches a partial application receipt authorizing mutation."""

        self.assertTrue(hasattr(self.store, "commit_delivery_application"))
        _, preflight, shortlist, _, _ = self.acknowledge_handoff()
        gate = self.store.begin_turn_gate(
            session_id=SESSION_ID,
            turn_id="turn-next",
            context_epoch=0,
            intent_epoch=0,
            active_generation=preflight.generation,
            gate_id="gate-next",
        )
        self.store._connection.execute(  # noqa: SLF001 - transaction fault injection
            """
            CREATE TRIGGER fail_active_item_insert
            BEFORE INSERT ON recall_active_injected_items
            BEGIN
                SELECT RAISE(ABORT, 'forced application rollback');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_delivery_application(
                session_id=SESSION_ID,
                turn_id="turn-next",
                gate_id=gate.gate_id,
                delivery_id=DELIVERY_ID,
                submission=self.application(shortlist, ("applicable",)),
                now=NOW,
            )

        delivery = self.store.get_delivery(DELIVERY_ID)
        self.assertEqual("host_delivered", delivery.state)
        self.assertIsNone(delivery.application_receipt_id)
        self.assertEqual("pending", self.store.get_turn_gate(SESSION_ID, "turn-next").state)
        self.assertEqual("activating", self.store.get_session(SESSION_ID).state)
        self.assertEqual((), self.store.list_active_items(SESSION_ID))

    def test_committed_application_reopens_and_replays_without_duplicate_transition(
        self,
    ) -> None:
        """This catches restart rerunning application or duplicating active rows."""

        self.assertTrue(hasattr(self.store, "commit_delivery_application"))
        _, preflight, shortlist, _, _ = self.acknowledge_handoff()
        gate = self.store.begin_turn_gate(
            session_id=SESSION_ID,
            turn_id="turn-next",
            context_epoch=0,
            intent_epoch=0,
            active_generation=preflight.generation,
            gate_id="gate-next",
        )
        submission = self.application(shortlist, ("applicable",))
        committed = self.store.commit_delivery_application(
            session_id=SESSION_ID,
            turn_id="turn-next",
            gate_id=gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=submission,
            now=NOW,
        )
        self.store.close()
        reopened = RecallHostStore.open(self.path)
        self.store = reopened
        self.addCleanup(reopened.close)

        replay = reopened.commit_delivery_application(
            session_id=SESSION_ID,
            turn_id="turn-next",
            gate_id=gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=submission,
            now=NOW + timedelta(minutes=1),
        )

        self.assertEqual(committed, replay)
        self.assertEqual(1, len(reopened.list_active_items(SESSION_ID)))
        self.assertEqual(1, reopened.get_session(SESSION_ID).intent_epoch)

    def test_legacy_attempts_remain_readable_but_cannot_authorize_v1_delivery(
        self,
    ) -> None:
        """This catches silently upgrading an old committed consent row."""

        intent, preflight, _ = self.handoff_values()
        with self.assertRaises(ValueError):
            self.store.create_activation_attempt(
                session_id="session-missing-preflight",
                turn_id="turn-missing-preflight",
                cwd="/tmp/recall",
                repository_id=preflight.repository_id,
                repository_display_name=preflight.repository_display_name,
                attempt_id="activation_" + "1" * 32,
                now=NOW,
                expires_at=NOW + timedelta(minutes=15),
                plugin_root=None,
                intent=intent,
            )
        legacy = self.store.create_activation_attempt(
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            cwd="/tmp/recall",
            repository_id=preflight.repository_id,
            repository_display_name=preflight.repository_display_name,
            attempt_id="activation_" + "2" * 32,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
        )
        self.store.attach_activation_card(legacy.attempt_id, ui_digest="a" * 64)
        self.store.decide_activation_attempt(legacy.attempt_id, action="enable", now=NOW)

        self.assertIsNone(self.store.get_activation_attempt(legacy.attempt_id).protocol_version)
        self.assertIsNone(self.store.get_activation_attempt(legacy.attempt_id).preflight)
        self.assertIsNone(self.store.get_session(SESSION_ID).protocol_version)
        with self.assertRaises(RecallGateConflict):
            self.store.begin_delivery(
                attempt_id=legacy.attempt_id,
                delivery_id=DELIVERY_ID,
                claim_token="claim_" + "8" * 32,
                now=NOW,
                claim_expires_at=NOW + timedelta(seconds=30),
            )

    def test_v1_attempt_cannot_use_legacy_enable_without_a_delivery(self) -> None:
        """This catches the old decision API bypassing the v1 delivery transaction."""

        attempt, _, _ = self.create_handoff_attempt()

        with self.assertRaises(RecallGateConflict):
            self.store.decide_activation_attempt(
                attempt.attempt_id,
                action="enable",
                now=NOW,
            )

        self.assertEqual(
            "pending_confirmation",
            self.store.get_activation_attempt(attempt.attempt_id).state,
        )
        self.assertIsNone(self.store.get_session(SESSION_ID))

    def test_v1_activating_session_rejects_legacy_activation_and_gate_commit(
        self,
    ) -> None:
        """This catches legacy state APIs bypassing the application receipt."""

        attempt, preflight, shortlist = self.create_handoff_attempt()
        claim = self.store.begin_delivery(
            attempt_id=attempt.attempt_id,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "a" * 32,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )

        with self.assertRaises(RecallGateConflict):
            self.store.bind_activation(
                session_id=SESSION_ID,
                turn_id="turn-legacy",
                cwd="/tmp/recall",
                binding_id="activation-legacy-bypass",
                now=NOW,
            )
        self.assertEqual("activating", self.store.get_session(SESSION_ID).state)
        self.assertEqual("preparing", self.store.get_delivery(DELIVERY_ID).state)
        with self.assertRaises(RecallGateConflict):
            self.store.begin_turn_gate(
                session_id=SESSION_ID,
                turn_id="turn-next",
                context_epoch=0,
                intent_epoch=0,
                active_generation=preflight.generation,
                gate_id="gate-next",
            )

        context_text = build_handoff_context(DELIVERY_ID, preflight, shortlist)
        prepared = self.store.commit_prepared_delivery(
            delivery_id=DELIVERY_ID,
            claim_token=claim.claim_token,
            shortlist=shortlist,
            context_text=context_text,
            now=NOW,
        )
        self.store.ack_delivery(
            delivery_id=DELIVERY_ID,
            context_digest=prepared.context_digest,
            now=NOW,
        )
        gate = self.store.begin_turn_gate(
            session_id=SESSION_ID,
            turn_id="turn-next",
            context_epoch=0,
            intent_epoch=0,
            active_generation=preflight.generation,
            gate_id="gate-next",
        )
        with self.assertRaises(RecallGateConflict):
            self.store.commit_turn_gate(
                session_id=SESSION_ID,
                turn_id="turn-next",
                gate_id=gate.gate_id,
                result=_result(intent_digest="forged-intent"),
                active_set_digest="forged-active-set",
            )
        self.assertEqual("pending", self.store.get_turn_gate(SESSION_ID, "turn-next").state)
        self.assertEqual("activating", self.store.get_session(SESSION_ID).state)

        applied = self.store.commit_delivery_application(
            session_id=SESSION_ID,
            turn_id="turn-next",
            gate_id=gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=self.application(shortlist, ("applicable",)),
            now=NOW,
        )

        self.assertEqual("application_committed", applied.state)
        self.assertEqual("committed", self.store.get_turn_gate(SESSION_ID, "turn-next").state)
        self.assertEqual("active", self.store.get_session(SESSION_ID).state)

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
            (root / "skills/zdecision").mkdir(parents=True)
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
            hooks_path = root / "hooks/hooks.json"
            hooks_path.parent.mkdir()
            hooks_path.write_text(
                (
                    Path(__file__).resolve().parents[1]
                    / "plugins/zdecision/hooks/hooks.json"
                ).read_text("utf-8"),
                "utf-8",
            )
            (root / "skills/zdecision/SKILL.md").write_text(name, "utf-8")
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

    def test_late_internal_binding_blocks_every_v1_delivery_transition(self) -> None:
        """This catches a late Capture identity receiving or applying Recall bytes."""

        intent, preflight, shortlist = self.handoff_values()

        def freeze(digit: str):
            session_id = f"internal-session-{digit}"
            turn_id = f"internal-turn-{digit}"
            attempt_id = "activation_" + digit * 32
            delivery_id = "delivery_" + digit * 32
            attempt = self.store.create_activation_attempt(
                session_id=session_id,
                turn_id=turn_id,
                cwd="/tmp/recall",
                repository_id=preflight.repository_id,
                repository_display_name=preflight.repository_display_name,
                attempt_id=attempt_id,
                now=NOW,
                expires_at=NOW + timedelta(minutes=15),
                plugin_root=None,
                intent=intent,
                preflight=preflight,
            )
            self.store.attach_activation_card(attempt_id, ui_digest="a" * 64)
            return session_id, turn_id, attempt_id, delivery_id

        def begin(attempt_id: str, delivery_id: str, digit: str):
            return self.store.begin_delivery(
                attempt_id=attempt_id,
                delivery_id=delivery_id,
                claim_token="claim_" + digit * 32,
                now=NOW,
                claim_expires_at=NOW + timedelta(seconds=30),
            )

        def prepare(delivery_id: str, claim_token: str):
            return self.store.commit_prepared_delivery(
                delivery_id=delivery_id,
                claim_token=claim_token,
                shortlist=shortlist,
                context_text=build_handoff_context(
                    delivery_id, preflight, shortlist
                ),
                now=NOW,
            )

        def bind_internal(session_id: str, digit: str) -> None:
            self.store.bind_internal_thread(
                thread_id=session_id,
                parent_thread_id="parent-session",
                purpose="capture",
                operation_id=f"late-internal-{digit}",
                now=NOW,
            )

        session, _, attempt, delivery = freeze("1")
        bind_internal(session, "1")
        with self.assertRaises(RecallGateConflict):
            begin(attempt, delivery, "1")
        self.assertIsNone(self.store.delivery_for_attempt(attempt))

        session, _, attempt, delivery = freeze("2")
        claim = begin(attempt, delivery, "2")
        bind_internal(session, "2")
        with self.assertRaises(RecallGateConflict):
            prepare(delivery, claim.claim_token)
        self.assertEqual("preparing", self.store.get_delivery(delivery).state)

        session, _, attempt, delivery = freeze("3")
        claim = begin(attempt, delivery, "3")
        prepared = prepare(delivery, claim.claim_token)
        bind_internal(session, "3")
        with self.assertRaises(RecallGateConflict):
            self.store.ack_delivery(
                delivery_id=delivery,
                context_digest=prepared.context_digest,
                now=NOW,
            )
        with self.assertRaises(RecallGateConflict):
            self.store.mark_delivery_unknown(
                delivery_id=delivery,
                now=NOW + timedelta(seconds=30),
            )
        with self.assertRaises(RecallGateConflict):
            self.store.claim_delivery_retry(
                delivery_id=delivery,
                claim_token="claim_" + "a" * 32,
                now=NOW + timedelta(seconds=30),
                claim_expires_at=NOW + timedelta(seconds=60),
            )
        self.assertEqual("delivery_claimed", self.store.get_delivery(delivery).state)

        session, _, attempt, delivery = freeze("4")
        claim = begin(attempt, delivery, "4")
        prepared = prepare(delivery, claim.claim_token)
        self.store.ack_delivery(
            delivery_id=delivery,
            context_digest=prepared.context_digest,
            now=NOW,
        )
        gate = self.store.begin_turn_gate(
            session_id=session,
            turn_id="application-turn",
            context_epoch=0,
            intent_epoch=0,
            active_generation=preflight.generation,
            gate_id="application-gate",
        )
        bind_internal(session, "4")
        submission = RecallApplicationSubmission.from_dict(
            {
                **self.application(shortlist, ("applicable",)).to_dict(),
                "delivery_id": delivery,
            }
        )
        with self.assertRaises(RecallGateConflict):
            self.store.commit_delivery_application(
                session_id=session,
                turn_id="application-turn",
                gate_id=gate.gate_id,
                delivery_id=delivery,
                submission=submission,
                now=NOW,
            )
        terminal = self.store.get_delivery(delivery)
        self.assertEqual("host_delivered", terminal.state)
        self.assertIsNone(terminal.application_receipt_id)
        self.assertEqual((), self.store.list_active_items(session))

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

    def test_open_additively_migrates_legacy_recall_schema_and_rows(self) -> None:
        """This catches rebuilding or silently authorizing pre-v1 Recall rows."""

        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE recall_sessions (
                    session_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    authorization_turn_id TEXT NOT NULL, cwd TEXT NOT NULL,
                    context_epoch INTEGER NOT NULL, intent_epoch INTEGER NOT NULL,
                    active_intent_digest TEXT, active_set_digest TEXT,
                    last_gate_turn_id TEXT, ended_at TEXT, resumed_at TEXT
                );
                CREATE TABLE recall_activation_attempts (
                    attempt_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL, cwd TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    repository_display_name TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    plugin_root TEXT, plugin_bundle_digest TEXT, ui_digest TEXT,
                    result_digest TEXT, UNIQUE(session_id, turn_id)
                );
                """
            )
            connection.execute(
                """
                INSERT INTO recall_activation_attempts VALUES (
                    ?, ?, ?, ?, ?, ?, 'committed', ?, ?, NULL, NULL, ?, ?
                )
                """,
                (
                    "activation_" + "3" * 32,
                    "legacy-session",
                    "legacy-turn",
                    "/tmp/legacy",
                    "repo_" + "3" * 32,
                    "legacy",
                    "2026-08-06T03:00:00.000000Z",
                    "2026-08-06T03:15:00.000000Z",
                    "a" * 64,
                    "b" * 64,
                ),
            )
            connection.commit()

        migrated = RecallHostStore.open(legacy_path)
        self.addCleanup(migrated.close)
        with closing(sqlite3.connect(legacy_path)) as connection:
            attempt_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(recall_activation_attempts)"
                )
            }
            session_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(recall_sessions)")
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertTrue(
            {"protocol_version", "preflight_json", "preflight_digest"}
            <= attempt_columns
        )
        self.assertTrue({"protocol_version", "repository_id"} <= session_columns)
        self.assertTrue(
            {"recall_deliveries", "recall_active_injected_items"} <= tables
        )
        legacy = migrated.get_activation_attempt("activation_" + "3" * 32)
        self.assertEqual("committed", legacy.state)
        self.assertIsNone(legacy.protocol_version)
        self.assertIsNone(legacy.preflight)
