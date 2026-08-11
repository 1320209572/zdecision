"""Behavior tests for idempotent Recall enable-and-prepare orchestration."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

from zdecision.agent.recall_handoff import RecallHandoffService
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.recall.handoff import (
    RecallApplicationSubmission,
    RecallPreflightClarification,
    RecallShortlist,
    RecalledDecision,
)
from zdecision.recall.session import RecallIntent

from tests.test_recall_handoff_contracts import (
    formal_decision,
    ready_preflight,
    valid_intent,
)


NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
ATTEMPT_ID = "activation_" + "1" * 32
DELIVERY_ID = "delivery_" + "2" * 32
UI_DIGEST = "a" * 64


class _CountingProvider:
    def __init__(self, shortlist: RecallShortlist) -> None:
        self.shortlist = shortlist
        self.preflight_result = None
        self.preflight_calls = 0
        self.retrieve_calls = 0
        self.on_retrieve = None

    def preflight(self, **_kwargs):
        self.preflight_calls += 1
        if self.preflight_result is None:
            raise AssertionError("unexpected preflight")
        return self.preflight_result

    def retrieve(self, preflight):
        self.retrieve_calls += 1
        if self.on_retrieve is not None:
            self.on_retrieve()
        return self.shortlist


class _WritingProvider(_CountingProvider):
    def __init__(self, shortlist: RecallShortlist, path: Path) -> None:
        super().__init__(shortlist)
        self.path = path

    def retrieve(self, preflight):
        with sqlite3.connect(self.path, timeout=0.05) as connection:
            connection.execute(
                "CREATE TABLE provider_transaction_probe(value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO provider_transaction_probe(value) VALUES ('outside')"
            )
        return super().retrieve(preflight)


class _FailingProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.retrieve_calls = 0

    def retrieve(self, preflight):
        self.retrieve_calls += 1
        raise self.error


class _ExpiringProvider(_CountingProvider):
    def __init__(self, shortlist: RecallShortlist, expire) -> None:
        super().__init__(shortlist)
        self.expire = expire

    def retrieve(self, preflight):
        result = super().retrieve(preflight)
        self.expire()
        return result


class RecallHandoffServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.path = Path(temporary_directory.name) / "zdecision.sqlite3"
        self.store = RecallHostStore.open(self.path)
        self.addCleanup(self.store.close)
        self.intent = valid_intent()
        self.preflight = ready_preflight(intent=self.intent)
        item = RecalledDecision.create(
            decision_space_id="space-product",
            revision=formal_decision(),
            match_reason="Exact product match",
        )
        self.shortlist = RecallShortlist.create(
            preflight=self.preflight,
            items=(item,),
        )
        self.store.create_activation_attempt(
            session_id="private-session",
            turn_id="private-turn",
            cwd="/tmp/recall-handoff-service",
            repository_id=self.preflight.repository_id,
            repository_display_name=self.preflight.repository_display_name,
            attempt_id=ATTEMPT_ID,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
            intent=self.intent,
            preflight=self.preflight,
        )
        self.store.attach_activation_card(ATTEMPT_ID, ui_digest=UI_DIGEST)
        self.provider = _CountingProvider(self.shortlist)
        self.current_time = NOW
        claim_numbers = count(1)
        self.service = RecallHandoffService(
            store=self.store,
            provider=self.provider,
            clock=lambda: self.current_time,
            delivery_id_factory=lambda _: DELIVERY_ID,
            claim_token_factory=lambda: f"claim_{next(claim_numbers):032x}",
        )

    def test_double_click_reuses_one_frozen_delivery_without_retrieval(self) -> None:
        """This catches duplicate retrieval or substituted bytes on app replay."""

        first = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )
        replay = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual("delivery_claimed", first["state"])
        self.assertEqual(1, self.provider.retrieve_calls)
        self.assertEqual(DELIVERY_ID, first["_meta"]["zdecision/delivery_id"])
        self.assertEqual(
            self.store.get_delivery(DELIVERY_ID).context_digest,
            first["_meta"]["zdecision/context_digest"],
        )
        self.assertEqual("delivery_in_progress", replay["code"])
        self.assertEqual(
            first["_meta"]["zdecision/delivery_id"],
            replay["_meta"]["zdecision/delivery_id"],
        )
        self.assertEqual(
            first["_meta"]["zdecision/context_digest"],
            replay["_meta"]["zdecision/context_digest"],
        )
        self.assertNotIn("zdecision/context_text", replay["_meta"])

    def test_concurrent_preparing_click_is_in_progress_without_retrieval(self) -> None:
        """This catches a second caller retrieving under another live claim."""

        self.store.begin_delivery(
            attempt_id=ATTEMPT_ID,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "a" * 32,
            current_ui_digest=UI_DIGEST,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )

        result = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual(
            {"state": "preparing", "code": "delivery_in_progress"}, result
        )
        self.assertEqual(0, self.provider.retrieve_calls)

    def test_expired_preparing_claim_is_taken_over_with_the_same_delivery_id(self) -> None:
        """This catches a crash after preparing permanently stranding consent."""

        self.store.begin_delivery(
            attempt_id=ATTEMPT_ID,
            delivery_id=DELIVERY_ID,
            claim_token="claim_" + "a" * 32,
            current_ui_digest=UI_DIGEST,
            now=NOW,
            claim_expires_at=NOW + timedelta(seconds=30),
        )
        self.current_time = NOW + timedelta(seconds=30)

        result = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual("delivery_claimed", result["state"])
        self.assertEqual(DELIVERY_ID, result["_meta"]["zdecision/delivery_id"])
        self.assertEqual(1, self.provider.retrieve_calls)

    def test_status_derives_unknown_and_only_enable_reclaims_frozen_bytes(self) -> None:
        """This catches status mutation or blind resend after an ack-lost crash."""

        first = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )
        frozen_context = first["_meta"]["zdecision/context_text"]
        self.current_time = NOW + timedelta(seconds=30)

        observed = self.service.status(attempt_id=ATTEMPT_ID)

        self.assertEqual("delivery_unknown", observed["state"])
        self.assertEqual("acknowledgement_expired", observed["code"])
        self.assertEqual(
            "delivery_claimed", self.store.get_delivery(DELIVERY_ID).state
        )
        self.assertNotIn("zdecision/context_text", observed["_meta"])

        retried = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual("delivery_claimed", retried["state"])
        self.assertEqual(frozen_context, retried["_meta"]["zdecision/context_text"])
        self.assertEqual(
            first["_meta"]["zdecision/snapshot_digest"],
            retried["_meta"]["zdecision/snapshot_digest"],
        )
        self.assertEqual(1, self.provider.retrieve_calls)

    def test_ack_requires_the_exact_attempt_delivery_and_context_digest(self) -> None:
        """This catches cross-attempt or substituted acknowledgements committing."""

        claimed = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        for arguments in (
            {
                "attempt_id": "activation_" + "9" * 32,
                "delivery_id": DELIVERY_ID,
                "context_digest": claimed["_meta"]["zdecision/context_digest"],
            },
            {
                "attempt_id": ATTEMPT_ID,
                "delivery_id": "delivery_" + "9" * 32,
                "context_digest": claimed["_meta"]["zdecision/context_digest"],
            },
            {
                "attempt_id": ATTEMPT_ID,
                "delivery_id": DELIVERY_ID,
                "context_digest": "9" * 64,
            },
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    {"state": "blocked", "code": "invalid_delivery"},
                    self.service.ack(**arguments),
                )
                self.assertEqual(
                    "delivery_claimed", self.store.get_delivery(DELIVERY_ID).state
                )

        acknowledged = self.service.ack(
            attempt_id=ATTEMPT_ID,
            delivery_id=DELIVERY_ID,
            context_digest=claimed["_meta"]["zdecision/context_digest"],
        )

        self.assertEqual("host_delivered", acknowledged["state"])
        self.assertEqual(DELIVERY_ID, acknowledged["_meta"]["zdecision/delivery_id"])
        self.assertEqual(
            claimed["_meta"]["zdecision/context_digest"],
            acknowledged["_meta"]["zdecision/context_digest"],
        )
        self.assertNotIn("zdecision/context_text", acknowledged["_meta"])
        self.assertEqual("host_delivered", self.store.get_delivery(DELIVERY_ID).state)

        recovered = self.service.status(attempt_id=ATTEMPT_ID)
        self.assertEqual(acknowledged, recovered)

    def test_provider_runs_after_the_delivery_transaction_commits(self) -> None:
        """This catches retrieval executing while SQLite holds the consent write."""

        provider = _WritingProvider(self.shortlist, self.path)
        self.service.provider = provider

        result = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual("delivery_claimed", result["state"])
        self.assertEqual(1, provider.retrieve_calls)
        row = self.store._connection.execute(  # noqa: SLF001 - transaction oracle
            "SELECT value FROM provider_transaction_probe"
        ).fetchone()
        self.assertEqual("outside", row[0])

    def test_provider_exception_is_bounded_and_leaves_session_activating(self) -> None:
        """This catches provider details escaping or consent becoming active."""

        provider = _FailingProvider(RuntimeError("private provider detail"))
        self.service.provider = provider

        result = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual(
            {"state": "blocked", "code": "delivery_prepare_failed"}, result
        )
        self.assertEqual(1, provider.retrieve_calls)
        self.assertEqual("preparing", self.store.get_delivery(DELIVERY_ID).state)
        self.assertEqual("activating", self.store.get_session("private-session").state)
        self.assertNotIn("private provider detail", repr(result))

    def test_all_decision_byte_limit_failure_is_bounded(self) -> None:
        """This catches oversized complete Decisions escaping the shared budget."""

        class OversizedProvider:
            retrieve_calls = 0

            def retrieve(inner_self, preflight):
                inner_self.retrieve_calls += 1
                oversized = RecalledDecision.create(
                    decision_space_id="space-product",
                    revision=formal_decision(claim="x" * 10_001),
                    match_reason="Oversized complete Decision",
                )
                return RecallShortlist.create(
                    preflight=preflight,
                    items=(oversized,),
                )

        provider = OversizedProvider()
        self.service.provider = provider

        result = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual(
            {"state": "blocked", "code": "delivery_prepare_failed"}, result
        )
        self.assertEqual(1, provider.retrieve_calls)
        self.assertEqual("preparing", self.store.get_delivery(DELIVERY_ID).state)

    def test_invalid_or_expired_attempt_fails_before_provider_work(self) -> None:
        """This catches a changed card or expired consent starting retrieval."""

        wrong_digest = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest="f" * 64,
        )
        self.current_time = NOW + timedelta(minutes=15)
        expired = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        expected = {"state": "blocked", "code": "invalid_confirmation"}
        self.assertEqual(expected, wrong_digest)
        self.assertEqual(expected, expired)
        self.assertEqual(0, self.provider.retrieve_calls)
        self.assertIsNone(self.store.delivery_for_attempt(ATTEMPT_ID))
        self.assertIsNone(self.store.get_session("private-session"))

    def test_mismatched_preflight_or_expired_commit_fails_closed(self) -> None:
        """This catches a provider substituting generation bytes or winning late."""

        mismatched_preflight = replace(
            self.preflight,
            generation=2,
            generation_digest="d" * 64,
        )
        mismatched = RecallShortlist.create(
            preflight=mismatched_preflight,
            items=(),
        )
        self.service.provider = _CountingProvider(mismatched)
        mismatch_result = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )
        self.assertEqual(
            {"state": "blocked", "code": "delivery_prepare_failed"},
            mismatch_result,
        )

        self.current_time = NOW + timedelta(seconds=30)
        self.service.provider = _ExpiringProvider(
            self.shortlist,
            lambda: setattr(
                self, "current_time", NOW + timedelta(seconds=61)
            ),
        )
        race_result = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual(
            {"state": "blocked", "code": "delivery_prepare_failed"},
            race_result,
        )
        self.assertEqual("preparing", self.store.get_delivery(DELIVERY_ID).state)

    def test_plugin_bundle_is_revalidated_after_provider_work(self) -> None:
        """This catches changed installed bytes being accepted after retrieval."""

        plugin_root = self.path.parent / "plugin"
        (plugin_root / ".codex-plugin").mkdir(parents=True)
        (plugin_root / "skills/zdecision").mkdir(parents=True)
        (plugin_root / ".codex-plugin/plugin.json").write_text(
            json.dumps(
                {
                    "name": "zdecision",
                    "skills": "./skills/",
                    "mcpServers": "./.mcp.json",
                }
            ),
            "utf-8",
        )
        (plugin_root / ".mcp.json").write_text(
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
        skill_path = plugin_root / "skills/zdecision/SKILL.md"
        skill_path.write_text("trusted bundle", "utf-8")
        attempt_id = "activation_" + "6" * 32
        delivery_id = "delivery_" + "7" * 32
        self.store.create_activation_attempt(
            session_id="bundle-session",
            turn_id="bundle-turn",
            cwd="/tmp/recall-handoff-bundle",
            repository_id=self.preflight.repository_id,
            repository_display_name=self.preflight.repository_display_name,
            attempt_id=attempt_id,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=str(plugin_root),
            intent=self.intent,
            preflight=self.preflight,
        )
        self.store.attach_activation_card(attempt_id, ui_digest=UI_DIGEST)
        provider = _ExpiringProvider(
            self.shortlist,
            lambda: skill_path.write_text("changed bundle", "utf-8"),
        )
        service = RecallHandoffService(
            store=self.store,
            provider=provider,
            clock=lambda: NOW,
            delivery_id_factory=lambda _: delivery_id,
            claim_token_factory=lambda: "claim_" + "8" * 32,
        )

        result = service.enable(
            attempt_id=attempt_id,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual(
            {"state": "blocked", "code": "delivery_prepare_failed"}, result
        )
        self.assertEqual(1, provider.retrieve_calls)
        self.assertEqual("preparing", self.store.get_delivery(delivery_id).state)

    def test_preflight_expiry_during_retrieval_fails_before_commit(self) -> None:
        """This catches a late shortlist committing after frozen authority expires."""

        expiring_preflight = replace(
            self.preflight,
            expires_at="2026-08-10T10:00:10Z",
        )
        shortlist = RecallShortlist.create(preflight=expiring_preflight, items=())
        attempt_id = "activation_" + "9" * 32
        delivery_id = "delivery_" + "a" * 32
        self.store.create_activation_attempt(
            session_id="expiring-session",
            turn_id="expiring-turn",
            cwd="/tmp/recall-handoff-expiring",
            repository_id=expiring_preflight.repository_id,
            repository_display_name=expiring_preflight.repository_display_name,
            attempt_id=attempt_id,
            now=NOW,
            expires_at=NOW + timedelta(minutes=15),
            plugin_root=None,
            intent=self.intent,
            preflight=expiring_preflight,
        )
        self.store.attach_activation_card(attempt_id, ui_digest=UI_DIGEST)
        current_time = [NOW]
        provider = _ExpiringProvider(
            shortlist,
            lambda: current_time.__setitem__(0, NOW + timedelta(seconds=10)),
        )
        service = RecallHandoffService(
            store=self.store,
            provider=provider,
            clock=lambda: current_time[0],
            delivery_id_factory=lambda _: delivery_id,
            claim_token_factory=lambda: "claim_" + "b" * 32,
        )

        result = service.enable(
            attempt_id=attempt_id,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual(
            {"state": "blocked", "code": "delivery_prepare_failed"}, result
        )
        self.assertEqual("preparing", self.store.get_delivery(delivery_id).state)

    def test_decline_is_terminal_without_delivery_or_provider_work(self) -> None:
        """This catches decline accidentally authorizing the v1 handoff."""

        first = self.service.decline(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )
        replay = self.service.decline(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )

        self.assertEqual("declined", first["state"])
        self.assertEqual(first, replay)
        self.assertEqual(0, self.provider.retrieve_calls)
        self.assertIsNone(self.store.delivery_for_attempt(ATTEMPT_ID))
        self.assertIsNone(self.store.get_session("private-session"))

    def test_apply_delegates_to_atomic_commit_and_returns_only_safe_summary(
        self,
    ) -> None:
        """This catches application output exposing frozen host coordinates."""

        claimed = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )
        self.service.ack(
            attempt_id=ATTEMPT_ID,
            delivery_id=DELIVERY_ID,
            context_digest=claimed["_meta"]["zdecision/context_digest"],
        )
        gate = self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="next-turn",
            context_epoch=0,
            intent_epoch=0,
            active_generation=self.preflight.generation,
            gate_id="gate-next",
        )
        decision = self.shortlist.items[0]
        submission = RecallApplicationSubmission.from_dict(
            {
                "delivery_id": DELIVERY_ID,
                "items": [
                    {
                        "decision_id": decision.revision.decision_id,
                        "revision": decision.revision.revision,
                        "digest": decision.digest,
                        "disposition": "applicable",
                        "reason": "It governs this feature.",
                    }
                ],
            }
        )

        result = self.service.apply(
            session_id="private-session",
            turn_id="next-turn",
            gate_id=gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=submission,
        )

        self.assertEqual(
            {
                "state",
                "disposition_counts",
                "application_receipt_id",
                "intent_epoch",
                "scope_titles",
            },
            set(result),
        )
        self.assertEqual("application_committed", result["state"])
        self.assertEqual(
            {
                "applicable": 1,
                "not_applicable": 0,
                "conflicting": 0,
                "uncertain": 0,
            },
            result["disposition_counts"],
        )
        self.assertEqual(["Recall handoff tests"], result["scope_titles"])
        self.assertEqual(1, result["intent_epoch"])
        self.assertTrue(result["application_receipt_id"].startswith("application_"))
        encoded = json.dumps(result, sort_keys=True)
        for private in (
            "private-session",
            "next-turn",
            "gate-next",
            DELIVERY_ID,
            "/tmp/recall-handoff-service",
            decision.digest,
        ):
            self.assertNotIn(private, encoded)

    def test_same_active_intent_commits_reuse_without_provider_work(self) -> None:
        """This catches an ordinary continuation rerunning Recall retrieval."""

        claimed = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )
        self.service.ack(
            attempt_id=ATTEMPT_ID,
            delivery_id=DELIVERY_ID,
            context_digest=claimed["_meta"]["zdecision/context_digest"],
        )
        initial_gate = self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="application-turn",
            context_epoch=0,
            intent_epoch=0,
            active_generation=self.preflight.generation,
            gate_id="gate-application",
        )
        decision = self.shortlist.items[0]
        self.service.apply(
            session_id="private-session",
            turn_id="application-turn",
            gate_id=initial_gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=RecallApplicationSubmission.from_dict(
                {
                    "delivery_id": DELIVERY_ID,
                    "items": [
                        {
                            "decision_id": decision.revision.decision_id,
                            "revision": decision.revision.revision,
                            "digest": decision.digest,
                            "disposition": "applicable",
                            "reason": "It governs this feature.",
                        }
                    ],
                }
            ),
        )
        gate = self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="continuation-turn",
            context_epoch=0,
            intent_epoch=1,
            active_generation=None,
            gate_id="gate-continuation",
        )
        retrieve_calls = self.provider.retrieve_calls

        result = self.service.gate_turn(
            session_id="private-session",
            turn_id="continuation-turn",
            gate_id=gate.gate_id,
            intent=self.intent,
        )

        self.assertEqual("reuse", result["state"])
        self.assertEqual(retrieve_calls, self.provider.retrieve_calls)
        self.assertEqual(
            "committed",
            self.store.get_turn_gate("private-session", "continuation-turn").state,
        )

    def test_changed_intent_returns_complete_frozen_shortlist_and_keeps_gate_pending(
        self,
    ) -> None:
        """This catches changed intent reuse or an opaque/non-frozen tool result."""

        self._commit_active_fixture()
        changed = RecallIntent.from_dict(
            {
                **self.intent.to_dict(),
                "feature_goal": "Implement a changed Recall feature",
                "repository_relative_paths": ["src/zdecision/agent/hooks.py"],
            }
        )
        changed_preflight = replace(self.preflight, intent=changed)
        changed_item = RecalledDecision.create(
            decision_space_id=changed_preflight.target_decision_space_ids[0],
            revision=replace(
                formal_decision(claim="The changed feature uses this decision."),
                decision_id="dec_" + "8" * 32,
            ),
            match_reason="Changed feature match",
        )
        self.provider.preflight_result = changed_preflight
        self.provider.shortlist = RecallShortlist.create(
            preflight=changed_preflight,
            items=(changed_item,),
        )
        gate = self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="changed-turn",
            context_epoch=0,
            intent_epoch=1,
            active_generation=None,
            gate_id="gate-changed",
        )
        retrieve_calls = self.provider.retrieve_calls

        result = self.service.gate_turn(
            session_id="private-session",
            turn_id="changed-turn",
            gate_id=gate.gate_id,
            intent=changed,
        )

        self.assertEqual("retrieve", result["state"])
        self.assertEqual([changed_item.to_dict()], result["decisions"])
        self.assertEqual(retrieve_calls + 1, self.provider.retrieve_calls)
        self.assertEqual("pending", self.store.get_turn_gate("private-session", "changed-turn").state)
        self.assertEqual("activating", self.store.get_session("private-session").state)

        reopened = RecallHostStore.open(self.path)
        self.addCleanup(reopened.close)
        restarted = RecallHandoffService(
            store=reopened,
            provider=self.provider,
            clock=lambda: self.current_time,
            delivery_id_factory=lambda _: "delivery_" + "f" * 32,
            claim_token_factory=lambda: "claim_" + "f" * 32,
        )
        replay = restarted.gate_turn(
            session_id="private-session",
            turn_id="changed-turn",
            gate_id=gate.gate_id,
            intent=changed,
        )

        self.assertEqual(result, replay)
        self.assertEqual(retrieve_calls + 1, self.provider.retrieve_calls)

    def test_validated_product_change_retires_old_items_before_retrieval(self) -> None:
        """This catches old-product Decisions surviving into new-product routing."""

        self._commit_active_fixture()
        changed = RecallIntent.from_dict(
            {
                **self.intent.to_dict(),
                "target_decision_space_ids": ["space-other"],
                "feature_goal": "Work on the other product",
            }
        )
        changed_preflight = replace(
            self.preflight,
            intent=changed,
            target_decision_space_ids=("space-other",),
            target_display_names=("Other Product",),
        )
        changed_item = RecalledDecision.create(
            decision_space_id="space-other",
            revision=replace(
                formal_decision(claim="The other product uses this decision."),
                decision_id="dec_" + "7" * 32,
            ),
            match_reason="Other product match",
        )
        self.provider.preflight_result = changed_preflight
        self.provider.shortlist = RecallShortlist.create(
            preflight=changed_preflight,
            items=(changed_item,),
        )
        observed = []
        self.provider.on_retrieve = lambda: observed.append(
            (
                self.store.get_session("private-session").state,
                self.store.list_active_items("private-session"),
            )
        )
        gate = self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="product-turn",
            context_epoch=0,
            intent_epoch=1,
            active_generation=None,
            gate_id="gate-product",
        )

        result = self.service.gate_turn(
            session_id="private-session",
            turn_id="product-turn",
            gate_id=gate.gate_id,
            intent=changed,
        )

        self.assertEqual("retrieve", result["state"])
        self.assertEqual([("activating", ())], observed)

    def test_ambiguous_changed_intent_returns_names_without_retrieval_or_replacement(
        self,
    ) -> None:
        """This catches ambiguity retrieving or replacing the current active set."""

        self._commit_active_fixture()
        changed = RecallIntent.from_dict(
            {**self.intent.to_dict(), "feature_goal": "Work on an ambiguous product"}
        )
        self.provider.preflight_result = RecallPreflightClarification(
            code="ambiguous_target",
            candidate_display_names=("Cloud", "Shared UI"),
        )
        gate = self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="ambiguous-turn",
            context_epoch=0,
            intent_epoch=1,
            active_generation=None,
            gate_id="gate-ambiguous",
        )
        before = self.store.list_active_items("private-session")
        retrieve_calls = self.provider.retrieve_calls

        result = self.service.gate_turn(
            session_id="private-session",
            turn_id="ambiguous-turn",
            gate_id=gate.gate_id,
            intent=changed,
        )

        self.assertEqual(
            {
                "state": "clarify_product",
                "candidate_display_names": ["Cloud", "Shared UI"],
            },
            result,
        )
        self.assertEqual(retrieve_calls, self.provider.retrieve_calls)
        self.assertEqual(before, self.store.list_active_items("private-session"))
        self.assertEqual("pending", self.store.get_turn_gate("private-session", "ambiguous-turn").state)

    def _commit_active_fixture(self) -> None:
        claimed = self.service.enable(
            attempt_id=ATTEMPT_ID,
            current_ui_digest=UI_DIGEST,
        )
        self.service.ack(
            attempt_id=ATTEMPT_ID,
            delivery_id=DELIVERY_ID,
            context_digest=claimed["_meta"]["zdecision/context_digest"],
        )
        gate = self.store.begin_turn_gate(
            session_id="private-session",
            turn_id="application-turn",
            context_epoch=0,
            intent_epoch=0,
            active_generation=self.preflight.generation,
            gate_id="gate-application",
        )
        decision = self.shortlist.items[0]
        self.service.apply(
            session_id="private-session",
            turn_id="application-turn",
            gate_id=gate.gate_id,
            delivery_id=DELIVERY_ID,
            submission=RecallApplicationSubmission.from_dict(
                {
                    "delivery_id": DELIVERY_ID,
                    "items": [
                        {
                            "decision_id": decision.revision.decision_id,
                            "revision": decision.revision.revision,
                            "digest": decision.digest,
                            "disposition": "applicable",
                            "reason": "It governs this feature.",
                        }
                    ],
                }
            ),
        )
