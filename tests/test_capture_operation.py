from __future__ import annotations

import tempfile
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path

from tests.test_inventory import VALID_INVENTORY
from zdecision.agent.capture_operation_store import CaptureOperationStore
from zdecision.capture.on_demand import (
    FrozenCaptureInput,
    FrozenCaptureRouteContext,
    ValidatedCaptureResult,
)
from zdecision.capture.templates import TemplateCatalog


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"
NOW = "2026-07-31T08:00:00Z"


def route_context(**overrides: object) -> FrozenCaptureRouteContext:
    values: dict[str, object] = {
        "decision_space_id": "dsp_" + "8" * 32,
        "decision_space_kind": "product",
        "decision_space_name": "ZDecision",
        "route_id": "drr_" + "9" * 32,
        "route_configuration_version": 1,
        "compatibility_product_id": "prod_" + "a" * 32,
        "matched_path_digest": "b" * 64,
    }
    values.update(overrides)
    return FrozenCaptureRouteContext(**values)


def valid_candidate(claim: str = "Only page requests authorize capture.") -> dict[str, object]:
    return {
        "product": "ZDecision",
        "claim": claim,
        "future_action": "Run capture only for the frozen page request.",
        "scope": {
            "summary": "On-demand Candidate capture",
            "repositories": ["zdecision"],
            "paths": [],
        },
        "invalidation_conditions": ["The authorization boundary changes."],
    }


def frozen_input(**overrides: object) -> FrozenCaptureInput:
    template = overrides.pop(
        "template",
        TemplateCatalog(TEMPLATE_ROOT, ENVELOPE_ROOT).render(
            "business", "ZDecision"
        ),
    )
    values: dict[str, object] = {
        "request_id": "crq_" + "1" * 32,
        "repository_id": "repo_" + "2" * 32,
        "source_key": "src_" + "3" * 32,
        "session_id": "019fb100-0000-7000-8000-000000000001",
        "cwd": str(REPOSITORY_ROOT),
        "lineage": "lin_" + "4" * 32,
        "previous_handled_turn_id": "019fb100-0000-7000-8000-000000000002",
        "upper_turn_id": "019fb100-0000-7000-8000-000000000003",
        "source_fingerprint": "5" * 64,
        "product": "ZDecision",
        "template": template,
        "model_profile_id": "fmp_" + "6" * 32,
        "model_id": "gpt-5.4",
        "reasoning_effort": "medium",
        "model_discovery_digest": "7" * 64,
        "model_discovered_at": "2026-07-31T07:59:00Z",
        "protocol_revision": "extractor-v3",
        "route_context": route_context(),
    }
    values["protocol_revision"] = "extractor-v4"
    values.update(overrides)
    return FrozenCaptureInput.create(**values)


def validated_result(
    frozen: FrozenCaptureInput | None = None,
    *,
    claim: str = "Only page requests authorize capture.",
) -> ValidatedCaptureResult:
    selected = frozen or frozen_input()
    return ValidatedCaptureResult.create(
        selected,
        VALID_INVENTORY,
        {"candidates": [valid_candidate(claim)]},
    )


class FrozenCaptureInputTests(unittest.TestCase):
    def test_operation_identity_binds_every_frozen_input(self) -> None:
        first = frozen_input()
        replay = frozen_input()

        self.assertEqual(first.operation_id, replay.operation_id)
        self.assertEqual(4, first.record_version)

        changes: tuple[dict[str, object], ...] = (
            {"request_id": "crq_" + "8" * 32},
            {"repository_id": "repo_" + "8" * 32},
            {"source_key": "src_" + "8" * 32},
            {"session_id": "019fb100-0000-7000-8000-000000000008"},
            {"cwd": str(REPOSITORY_ROOT.parent)},
            {"lineage": "lin_" + "8" * 32},
            {"previous_handled_turn_id": None},
            {"upper_turn_id": "019fb100-0000-7000-8000-000000000008"},
            {"source_fingerprint": "8" * 64},
            {"product": "Another Product"},
            {
                "template": replace(
                    first.template,
                    template_source_sha256="8" * 64,
                )
            },
            {"model_profile_id": "fmp_" + "8" * 32},
            {"model_id": "gpt-5.5"},
            {"reasoning_effort": "high"},
            {"model_discovery_digest": "8" * 64},
            {"model_discovered_at": "2026-07-31T08:00:00Z"},
            {"protocol_revision": "extractor-v4-test"},
            {
                "route_context": route_context(
                    decision_space_id="dsp_" + "c" * 32
                )
            },
        )
        for change in changes:
            with self.subTest(change=tuple(change)):
                self.assertNotEqual(
                    first.operation_id,
                    frozen_input(**change).operation_id,
                )

    def test_records_round_trip_with_exact_fields(self) -> None:
        frozen = frozen_input()
        result = validated_result(frozen)

        self.assertEqual(frozen, FrozenCaptureInput.from_dict(frozen.to_dict()))
        self.assertEqual(
            result,
            ValidatedCaptureResult.from_dict(result.to_dict()),
        )
        with self.assertRaises(ValueError):
            FrozenCaptureInput.from_dict({**frozen.to_dict(), "extra": True})
        with self.assertRaises(ValueError):
            ValidatedCaptureResult.from_dict(
                {**result.to_dict(), "extra": True}
            )


class CaptureOperationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "capture-operations.sqlite3"
        )
        self.store = CaptureOperationStore.open(self.database_path)
        self.addCleanup(self.store.close)

    def test_exact_operation_reopens_after_process_restart(self) -> None:
        frozen = frozen_input()
        first = self.store.ensure_operation(frozen)
        self.store.close()
        self._cleanups.pop()

        self.store = CaptureOperationStore.open(self.database_path)
        self.addCleanup(self.store.close)
        replay = self.store.ensure_operation(frozen)

        self.assertEqual(first, replay)
        self.assertEqual(
            replay,
            self.store.operation_for_source(
                frozen.request_id, frozen.source_key
            ),
        )

    def test_old_source_unique_migrates_for_two_leaf_operations(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        with connection:
            connection.execute(
                """
                CREATE TABLE capture_operations (
                    operation_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    frozen_json TEXT NOT NULL,
                    frozen_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_generation INTEGER NOT NULL,
                    winner_generation INTEGER,
                    committed_result_json TEXT,
                    committed_result_digest TEXT,
                    failure_code TEXT,
                    UNIQUE(request_id, source_key)
                )
                """
            )
        connection.close()

        migrated = CaptureOperationStore.open(legacy_path)
        self.addCleanup(migrated.close)
        first = frozen_input()
        second = frozen_input(
            route_context=route_context(
                decision_space_id="dsp_" + "c" * 32,
                route_id="drr_" + "d" * 32,
            )
        )

        migrated.ensure_operation(first)
        migrated.ensure_operation(second)

        rows = migrated._connection.execute(
            "SELECT operation_id FROM capture_operations"
        ).fetchall()
        self.assertEqual(2, len(rows))

    def test_attempt_generations_increase_monotonically(self) -> None:
        operation = self.store.ensure_operation(frozen_input())
        first = self.store.begin_attempt(operation.operation_id, NOW)
        self.store.abandon_attempt(first.attempt_id, "transport_error", NOW)
        second = self.store.begin_attempt(operation.operation_id, NOW)

        self.assertEqual(1, first.generation)
        self.assertEqual(2, second.generation)
        self.assertNotEqual(first.attempt_id, second.attempt_id)

    def test_validated_result_survives_restart_before_cas(self) -> None:
        frozen = frozen_input()
        operation = self.store.ensure_operation(frozen)
        attempt = self.store.begin_attempt(operation.operation_id, NOW)
        expected = validated_result(frozen)
        self.store.store_validated_attempt(attempt.attempt_id, expected, NOW)
        self.store.close()
        self._cleanups.pop()

        self.store = CaptureOperationStore.open(self.database_path)
        self.addCleanup(self.store.close)
        committed = self.store.commit_attempt(attempt.attempt_id)

        self.assertEqual("committed", committed.operation.status)
        self.assertEqual(expected, committed.result)
        self.assertEqual(
            expected,
            self.store.committed_result(operation.operation_id),
        )

    def test_late_generation_cannot_commit(self) -> None:
        frozen = frozen_input()
        operation = self.store.ensure_operation(frozen)
        first = self.store.begin_attempt(operation.operation_id, NOW)
        self.store.abandon_attempt(
            first.attempt_id, "fork_result_unknown", NOW
        )
        second = self.store.begin_attempt(operation.operation_id, NOW)

        self.store.store_validated_attempt(
            first.attempt_id, validated_result(frozen, claim="old"), NOW
        )
        old_commit = self.store.commit_attempt(first.attempt_id)
        self.assertEqual("superseded", old_commit.attempt.state)
        self.assertIsNone(old_commit.result)

        self.store.store_validated_attempt(
            second.attempt_id, validated_result(frozen, claim="new"), NOW
        )
        winner = self.store.commit_attempt(second.attempt_id)
        self.assertEqual("committed", winner.operation.status)
        self.assertEqual("new", winner.result.observations[0].content.claim)

    def test_exact_winner_replay_returns_existing_receipt(self) -> None:
        frozen = frozen_input()
        operation = self.store.ensure_operation(frozen)
        attempt = self.store.begin_attempt(operation.operation_id, NOW)
        result = validated_result(frozen)
        self.store.store_validated_attempt(attempt.attempt_id, result, NOW)

        first = self.store.commit_attempt(attempt.attempt_id)
        replay = self.store.commit_attempt(attempt.attempt_id)

        self.assertEqual(first, replay)
        self.assertEqual("accepted", replay.attempt.state)

    def test_different_late_result_never_replaces_winner(self) -> None:
        frozen = frozen_input()
        operation = self.store.ensure_operation(frozen)
        first = self.store.begin_attempt(operation.operation_id, NOW)
        self.store.abandon_attempt(first.attempt_id, "retry", NOW)
        winner_attempt = self.store.begin_attempt(operation.operation_id, NOW)
        winner_result = validated_result(frozen, claim="winner")
        self.store.store_validated_attempt(
            winner_attempt.attempt_id, winner_result, NOW
        )
        self.store.commit_attempt(winner_attempt.attempt_id)

        self.store.store_validated_attempt(
            first.attempt_id, validated_result(frozen, claim="late"), NOW
        )
        late = self.store.commit_attempt(first.attempt_id)

        self.assertEqual("superseded", late.attempt.state)
        self.assertEqual(winner_result, late.result)
        self.assertEqual(
            winner_result,
            self.store.committed_result(operation.operation_id),
        )

    def test_known_terminal_thread_is_pending_until_archived(self) -> None:
        operation = self.store.ensure_operation(frozen_input())
        attempt = self.store.begin_attempt(operation.operation_id, NOW)
        attached = self.store.attach_thread(attempt.attempt_id, "thread-known")
        self.store.abandon_attempt(attached.attempt_id, "turn_unknown", NOW)

        pending = self.store.pending_archives()
        self.assertEqual((attempt.attempt_id,), tuple(x.attempt_id for x in pending))
        self.assertEqual("pending", pending[0].archive_state)

        archived = self.store.mark_archived(attempt.attempt_id)
        self.assertEqual("archived", archived.archive_state)
        self.assertEqual((), self.store.pending_archives())

    def test_unknown_thread_requires_no_archive(self) -> None:
        operation = self.store.ensure_operation(frozen_input())
        attempt = self.store.begin_attempt(operation.operation_id, NOW)
        self.store.abandon_attempt(attempt.attempt_id, "fork_unknown", NOW)

        self.assertEqual((), self.store.pending_archives())

    def test_stage_turns_attach_idempotently_and_conflicts_fail(self) -> None:
        operation = self.store.ensure_operation(frozen_input())
        attempt = self.store.begin_attempt(operation.operation_id, NOW)
        self.store.attach_thread(attempt.attempt_id, "thread-1")

        inventory = self.store.attach_turn(
            attempt.attempt_id, "inventory", "turn-1"
        )
        replay = self.store.attach_turn(
            attempt.attempt_id, "inventory", "turn-1"
        )
        extraction = self.store.attach_turn(
            attempt.attempt_id, "extraction", "turn-2"
        )

        self.assertEqual(inventory, replay)
        self.assertEqual("turn-2", extraction.extraction_turn_id)
        with self.assertRaises(ValueError):
            self.store.attach_turn(
                attempt.attempt_id, "inventory", "turn-other"
            )


if __name__ == "__main__":
    unittest.main()
