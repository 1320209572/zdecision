"""Contract tests for canonical Recall handoff values."""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path

from zdecision.capture.models import CandidateContent, SourceCheckpoint
from zdecision.capture.reviews import ApprovalRef
from zdecision.ids import decision_id, product_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.handoff import (
    RecallApplicationSubmission,
    RecallPreflightClarification,
    RecallPreflightReady,
    RecallPreflightUnavailable,
    RecallShortlist,
    RecalledDecision,
    build_handoff_context,
)
from zdecision.recall.session import RecallIntent
from zdecision.recall.provider import (
    RecallProviderUnavailable,
    UnavailableRecallProvider,
)
from zdecision.registry.models import DecisionRevision, DecisionSeed


def valid_intent() -> RecallIntent:
    return RecallIntent.from_dict(
        {
            "target_decision_space_ids": ["space-product"],
            "explicit_multi_space": False,
            "feature_goal": "Add the bounded Recall handoff",
            "domain_objects": ["Recall"],
            "repository_relative_paths": ["src/zdecision/recall"],
            "constraints": ["local only"],
            "exclusions": ["central writes"],
        }
    )


def formal_decision(*, claim: str = "The handoff remains canonical.") -> DecisionRevision:
    name = "ZDecision"
    identifier = product_id(name)
    candidate_id = "cand_" + "3" * 32 + "_01"
    return DecisionRevision.from_seed(
        DecisionSeed(
            candidate_id=candidate_id,
            decision_id=decision_id(candidate_id, identifier),
            product_id=identifier,
            product_name=name,
            content=CandidateContent(
                product=name,
                claim=claim,
                future_action="Preserve the exact bounded contract.",
                scope_summary="Recall handoff tests",
                repositories=("https://example.invalid/zdecision.git",),
                paths=("src/zdecision/recall",),
                invalidation_conditions=("The contract changes.",),
            ),
            source=SourceCheckpoint("thread-source", "turn-source"),
            review_approval=ApprovalRef(
                actor="user",
                thread_id="thread-review",
                turn_id="turn-review",
                recorded_at="2026-08-10T10:00:00Z",
            ),
        ),
        "pub_" + "4" * 32,
    )


def ready_preflight(*, intent: RecallIntent) -> RecallPreflightReady:
    return RecallPreflightReady(
        repository_id="repo_" + "1" * 32,
        repository_display_name="ZDecision repository",
        intent=intent,
        target_decision_space_ids=("space-product",),
        target_display_names=("Product decisions",),
        catalog_digest="a" * 64,
        generation=1,
        generation_digest="b" * 64,
        retrieval_profile_digest="c" * 64,
        index_generation=1,
        freshness="ready",
        expires_at="2026-08-10T11:00:00Z",
    )


class RecallHandoffContractTests(unittest.TestCase):
    def test_handoff_context_is_canonical_bounded_and_complete(self) -> None:
        """This catches an incomplete or executable Recall context handoff."""

        preflight = ready_preflight(intent=valid_intent())
        item = RecalledDecision.create(
            decision_space_id=preflight.target_decision_space_ids[0],
            revision=formal_decision(),
            match_reason="Exact product and capability match",
        )
        shortlist = RecallShortlist.create(preflight=preflight, items=(item,))
        text = build_handoff_context("delivery_" + "a" * 32, preflight, shortlist)
        payload = json.loads(text)

        self.assertEqual("ZDECISION_RECALL_HANDOFF", payload["marker"])
        self.assertEqual("recall-handoff-v1", payload["protocol"])
        self.assertEqual(item.digest, payload["decisions"][0]["digest"])
        self.assertEqual(
            item.revision.to_dict(), payload["decisions"][0]["formal_decision"]
        )
        self.assertNotIn("host-session-current", text)
        self.assertNotIn("host-turn-current", text)
        self.assertNotIn('"repository_id"', text)
        self.assertNotIn(str(Path.cwd()), text)
        self.assertEqual(text.encode("utf-8"), canonical_json_bytes(payload))

    def test_preflight_digest_is_stable_for_canonical_data(self) -> None:
        """This catches preflight digests that depend on mapping insertion order."""

        preflight = ready_preflight(intent=valid_intent())

        self.assertEqual(
            preflight.digest,
            hashlib.sha256(canonical_json_bytes(preflight.to_dict())).hexdigest(),
        )
        self.assertEqual(RecallPreflightReady.from_dict(preflight.to_dict()), preflight)

    def test_nonready_preflight_values_round_trip_exactly(self) -> None:
        """This catches malformed non-ready outcomes crossing the provider seam."""

        clarification = RecallPreflightClarification(
            code="clarification_required", message="Choose a Decision space."
        )
        unavailable = RecallPreflightUnavailable(code="recall_not_ready")

        self.assertEqual(
            RecallPreflightClarification.from_dict(clarification.to_dict()),
            clarification,
        )
        self.assertEqual(
            RecallPreflightUnavailable.from_dict(unavailable.to_dict()), unavailable
        )

    def test_shortlist_rejects_more_than_eight_items(self) -> None:
        """This catches an unbounded Decision handoff list."""

        preflight = ready_preflight(intent=valid_intent())
        item = RecalledDecision.create(
            decision_space_id="space-product",
            revision=formal_decision(),
            match_reason="Match",
        )

        with self.assertRaises(ValueError):
            RecallShortlist.create(preflight=preflight, items=(item,) * 9)

    def test_shortlist_rejects_unverifiable_or_duplicate_decisions(self) -> None:
        """This catches altered, foreign, or replayed formal Decisions."""

        preflight = ready_preflight(intent=valid_intent())
        item = RecalledDecision.create(
            decision_space_id="space-product",
            revision=formal_decision(),
            match_reason="Match",
        )
        truncated = RecalledDecision(
            decision_space_id=item.decision_space_id,
            revision=None,  # type: ignore[arg-type]
            digest=item.digest,
            match_reason=item.match_reason,
        )
        digest_mismatch = RecalledDecision(
            decision_space_id=item.decision_space_id,
            revision=item.revision,
            digest="0" * 64,
            match_reason=item.match_reason,
        )
        for invalid_items in (
            (digest_mismatch,),
            (truncated,),
            (item, item),
        ):
            with self.subTest(items=invalid_items):
                with self.assertRaises(ValueError):
                    RecallShortlist.create(preflight=preflight, items=invalid_items)
        with self.assertRaises(ValueError):
            RecallShortlist.create(
                preflight=replace(preflight, catalog_digest="d" * 64),
                items=(item,),
                preflight_digest=preflight.digest,
            )

    def test_shortlist_rejects_more_than_ten_thousand_canonical_decision_bytes(self) -> None:
        """This catches oversized canonical formal Decision payloads."""

        preflight = ready_preflight(intent=valid_intent())
        item = RecalledDecision.create(
            decision_space_id="space-product",
            revision=formal_decision(claim="x" * 10_001),
            match_reason="Match",
        )

        with self.assertRaises(ValueError):
            RecallShortlist.create(preflight=preflight, items=(item,))

    def test_application_submission_requires_exact_fields_and_dispositions(self) -> None:
        """This catches malformed or unrecognized Decision application reports."""

        decision = formal_decision()
        item = {
            "decision_id": decision.decision_id,
            "revision": 1,
            "digest": hashlib.sha256(canonical_json_bytes(decision.to_dict())).hexdigest(),
            "disposition": "applicable",
            "reason": "It governs this feature.",
        }
        value = {"delivery_id": "delivery_" + "a" * 32, "items": [item]}
        parsed = RecallApplicationSubmission.from_dict(value)

        self.assertEqual(parsed.to_dict(), value)
        for disposition in (
            "applicable",
            "not_applicable",
            "conflicting",
            "uncertain",
        ):
            with self.subTest(disposition=disposition):
                self.assertEqual(
                    RecallApplicationSubmission.from_dict(
                        {"delivery_id": value["delivery_id"], "items": [{**item, "disposition": disposition}]}
                    ).items[0].disposition,
                    disposition,
                )
        for malformed in (
            {"items": [item]},
            {**value, "unknown": True},
            {"delivery_id": value["delivery_id"], "items": [{**item, "disposition": "other"}]},
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    RecallApplicationSubmission.from_dict(malformed)

    def test_default_provider_fails_closed(self) -> None:
        """This catches accidental production retrieval before a provider exists."""

        provider = UnavailableRecallProvider()
        result = provider.preflight(
            repository_id="repo_" + "1" * 32,
            repository_display_name="ZDecision repository",
            intent=valid_intent(),
            now=datetime(2026, 8, 10, tzinfo=UTC),
        )

        self.assertEqual(result.code, "recall_not_ready")
        with self.assertRaisesRegex(
            RecallProviderUnavailable, "^Recall provider is unavailable$"
        ):
            provider.retrieve(ready_preflight(intent=valid_intent()))


if __name__ == "__main__":
    unittest.main()
