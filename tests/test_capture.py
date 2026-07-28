from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class CaptureModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_dir = Path(self.temp_dir.name)

    def capture_api(self) -> SimpleNamespace:
        try:
            from zdecision.capture.models import (
                Candidate,
                CandidateContent,
                CaptureRecord,
                SourceCheckpoint,
            )
            from zdecision.ids import capture_operation_id
            from zdecision.jsonio import atomic_write_json, canonical_json_bytes
            from zdecision.private_store.filesystem import (
                FilePrivateStore,
                InvalidPrivateObjectId,
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Capture model API is missing: {exc}")

        return SimpleNamespace(
            CaptureRecord=CaptureRecord,
            SourceCheckpoint=SourceCheckpoint,
            Candidate=Candidate,
            CandidateContent=CandidateContent,
            capture_operation_id=capture_operation_id,
            atomic_write_json=atomic_write_json,
            canonical_json_bytes=canonical_json_bytes,
            FilePrivateStore=FilePrivateStore,
            InvalidPrivateObjectId=InvalidPrivateObjectId,
        )

    def test_operation_id_is_stable_and_input_sensitive(self) -> None:
        """Catch random ids or ids that omit a checkpoint input."""
        api = self.capture_api()

        first = api.capture_operation_id("thread-a", "turn-7", "anheng")

        self.assertEqual(
            first,
            api.capture_operation_id("thread-a", "turn-7", "anheng"),
        )
        self.assertNotEqual(
            first,
            api.capture_operation_id("thread-a", "turn-8", "anheng"),
        )
        self.assertNotEqual(
            first,
            api.capture_operation_id("thread-a", "turn-7", "other-product"),
        )
        self.assertRegex(first, re.compile(r"^cap_[0-9a-f]{32}$"))

    def test_canonical_json_is_sorted_utf8_and_newline_terminated(self) -> None:
        """Catch unstable encoding that would change hashes across writes."""
        api = self.capture_api()

        encoded = api.canonical_json_bytes({"z": 1, "a": "安恒"})

        self.assertEqual(b'{"a":"\xe5\xae\x89\xe6\x81\x92","z":1}\n', encoded)

    def test_atomic_write_replaces_with_one_valid_json_document(self) -> None:
        """Catch append writes or temporary files left beside private state."""
        api = self.capture_api()
        path = self.state_dir / "object.json"

        api.atomic_write_json(path, {"version": 1})
        api.atomic_write_json(path, {"version": 2})

        self.assertEqual({"version": 2}, json.loads(path.read_text("utf-8")))
        self.assertEqual(["object.json"], [item.name for item in self.state_dir.iterdir()])

    def test_private_store_round_trips_typed_capture_without_raw_text(self) -> None:
        """Catch loss of typed fields or accidental transcript persistence."""
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        record = api.CaptureRecord.started(
            operation_id="cap_" + "a" * 32,
            source=api.SourceCheckpoint("thread-a", "turn-7"),
            product="anheng",
        )

        store.put_capture(record)
        loaded = store.get_capture(record.operation_id)

        self.assertEqual(record, loaded)
        serialized = (
            self.state_dir / "captures" / f"{record.operation_id}.json"
        ).read_text("utf-8")
        self.assertNotIn("raw_messages", serialized)
        self.assertNotIn("transcript", serialized)

    def test_capture_record_rejects_unknown_persisted_fields(self) -> None:
        """Catch silent acceptance of legacy or unowned state."""
        api = self.capture_api()
        record = api.CaptureRecord.started(
            operation_id="cap_" + "a" * 32,
            source=api.SourceCheckpoint("thread-a", "turn-7"),
            product="anheng",
        )
        payload = record.to_dict()
        payload["legacy_state"] = "must not load"

        with self.assertRaises(ValueError):
            api.CaptureRecord.from_dict(payload)

    def test_candidate_round_trip_preserves_typed_scope_only(self) -> None:
        """Catch flattening scope or adding source message text to Candidates."""
        api = self.capture_api()
        candidate = api.Candidate(
            candidate_id="cand_" + "a" * 32 + "_01",
            capture_id="cap_" + "a" * 32,
            ordinal=1,
            content=api.CandidateContent(
                product="anheng",
                claim="Use one Registry branch.",
                future_action="Publish reviewed decisions to main.",
                scope_summary="ZDecision V1",
                repositories=("https://example.com/zdecision.git",),
                paths=("decision-registry/",),
                invalidation_conditions=("V2 changes storage.",),
            ),
            source=api.SourceCheckpoint("thread-a", "turn-7"),
        )

        loaded = api.Candidate.from_dict(candidate.to_dict())

        self.assertEqual(candidate, loaded)
        self.assertNotIn("message", candidate.to_dict())
        self.assertNotIn("evidence", candidate.to_dict())

    def test_private_store_rejects_unsafe_object_id(self) -> None:
        """Catch private-state paths escaping their owned directory."""
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)

        with self.assertRaises(api.InvalidPrivateObjectId):
            store.get_capture("../outside")


def valid_candidate(
    *,
    claim: str = "Keep formal decisions on main.",
) -> dict[str, object]:
    return {
        "product": "anheng",
        "claim": claim,
        "future_action": "Write only reviewed decisions to decision-registry/.",
        "scope": {
            "summary": "ZDecision V1",
            "repositories": ["https://github.com/1320209572/zdecision.git"],
            "paths": ["decision-registry/"],
        },
        "invalidation_conditions": ["A later version moves Registry storage."],
    }


def extraction_with_two_candidates() -> dict[str, object]:
    return {
        "candidates": [
            valid_candidate(claim="Keep formal decisions on main."),
            valid_candidate(claim="Keep private Candidates outside Git."),
        ]
    }


class CaptureServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_dir = Path(self.temp_dir.name)

        from zdecision.capture.service import (
            CaptureForkAmbiguous,
            CaptureForkConflict,
            CaptureService,
            CaptureStateError,
            ExtractionValidationError,
        )
        from zdecision.private_store.filesystem import FilePrivateStore

        self.CaptureForkAmbiguous = CaptureForkAmbiguous
        self.CaptureForkConflict = CaptureForkConflict
        self.CaptureStateError = CaptureStateError
        self.ExtractionValidationError = ExtractionValidationError
        self.store = FilePrivateStore(self.state_dir)
        self.service = CaptureService(self.store)

    def prepared_and_attached(self) -> str:
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        self.service.attach_fork(plan.record.operation_id, "thread-fork")
        return plan.record.operation_id

    def test_prepare_returns_prompt_and_does_not_store_source_text(self) -> None:
        plan = self.service.prepare("thread-a", "turn-7", "anheng")

        self.assertEqual("prepared", plan.record.status)
        self.assertFalse(plan.replayed)
        self.assertIn('"candidates"', plan.extraction_prompt)
        self.assertNotIn("source_text", plan.record.to_dict())

    def test_prepare_prompt_uses_the_confirmed_product_verbatim(self) -> None:
        plan = self.service.prepare("thread-a", "turn-7", "安恒")

        self.assertIn('"product": "安恒"', plan.extraction_prompt)
        self.assertNotIn('"product": "anheng"', plan.extraction_prompt)

    def test_complete_requires_attached_fork(self) -> None:
        plan = self.service.prepare("thread-a", "turn-7", "anheng")

        with self.assertRaises(self.CaptureStateError):
            self.service.complete(plan.record.operation_id, {"candidates": []})

    def test_prepare_retry_before_fork_attach_is_ambiguous(self) -> None:
        self.service.prepare("thread-a", "turn-7", "anheng")

        with self.assertRaises(self.CaptureForkAmbiguous):
            self.service.prepare("thread-a", "turn-7", "anheng")

    def test_prepare_replays_attached_fork_without_creating_another(self) -> None:
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        attached = self.service.attach_fork(
            plan.record.operation_id,
            "thread-fork",
        )

        replay = self.service.prepare("thread-a", "turn-7", "anheng")

        self.assertTrue(replay.replayed)
        self.assertEqual(attached, replay.record)

    def test_attach_is_idempotent_only_for_the_same_fork(self) -> None:
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        self.service.attach_fork(plan.record.operation_id, "thread-fork")

        replay = self.service.attach_fork(plan.record.operation_id, "thread-fork")

        self.assertEqual("thread-fork", replay.fork_thread_id)
        with self.assertRaises(self.CaptureForkConflict):
            self.service.attach_fork(plan.record.operation_id, "different-fork")

    def test_zero_candidates_is_a_completed_result_and_replays(self) -> None:
        operation_id = self.prepared_and_attached()

        result = self.service.complete(operation_id, {"candidates": []})
        replay = self.service.prepare("thread-a", "turn-7", "anheng")

        self.assertEqual("completed", result.status)
        self.assertEqual((), result.candidate_ids)
        self.assertEqual(result.operation_id, replay.record.operation_id)
        self.assertTrue(replay.replayed)

    def test_complete_retry_returns_stored_result_without_new_candidates(self) -> None:
        operation_id = self.prepared_and_attached()
        completed = self.service.complete(
            operation_id,
            extraction_with_two_candidates(),
        )

        replay = self.service.complete(operation_id, {"candidates": []})

        self.assertEqual(completed, replay)
        self.assertEqual(2, len(tuple((self.state_dir / "candidates").iterdir())))

    def test_candidate_ids_follow_validated_result_order(self) -> None:
        operation_id = self.prepared_and_attached()

        result = self.service.complete(
            operation_id,
            extraction_with_two_candidates(),
        )

        self.assertEqual(
            (
                f"cand_{operation_id[4:]}_01",
                f"cand_{operation_id[4:]}_02",
            ),
            result.candidate_ids,
        )
        second = self.store.get_candidate(result.candidate_ids[1])
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(2, second.ordinal)
        self.assertEqual(
            "Keep private Candidates outside Git.",
            second.content.claim,
        )

    def test_extraction_rejects_unknown_fields_and_raw_evidence(self) -> None:
        operation_id = self.prepared_and_attached()

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete(
                operation_id,
                {
                    "candidates": [
                        {
                            **valid_candidate(),
                            "evidence_quote": "private source text",
                        }
                    ]
                },
            )

        self.assertFalse((self.state_dir / "candidates").exists())
        self.assertEqual("fork_attached", self.service.get(operation_id).status)

    def test_extraction_rejects_unknown_top_level_field(self) -> None:
        operation_id = self.prepared_and_attached()

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete(
                operation_id,
                {"candidates": [], "summary": "conversation summary"},
            )

    def test_extraction_rejects_empty_required_text(self) -> None:
        operation_id = self.prepared_and_attached()

        for field in ("product", "claim", "future_action"):
            candidate = valid_candidate()
            candidate[field] = " "
            with self.subTest(field=field):
                with self.assertRaises(self.ExtractionValidationError):
                    self.service.complete(
                        operation_id,
                        {"candidates": [candidate]},
                    )

        candidate = valid_candidate()
        scope = dict(candidate["scope"])
        scope["summary"] = ""
        candidate["scope"] = scope
        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete(operation_id, {"candidates": [candidate]})

    def test_extraction_rejects_a_silently_changed_product(self) -> None:
        operation_id = self.prepared_and_attached()
        candidate = valid_candidate()
        candidate["product"] = "安恒"

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete(operation_id, {"candidates": [candidate]})

        self.assertFalse((self.state_dir / "candidates").exists())

    def test_extraction_rejects_non_string_list_members(self) -> None:
        operation_id = self.prepared_and_attached()
        candidate = valid_candidate()
        scope = dict(candidate["scope"])
        scope["paths"] = ["decision-registry/", 7]
        candidate["scope"] = scope

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete(operation_id, {"candidates": [candidate]})

    def test_extraction_rejects_more_than_twenty_candidates(self) -> None:
        operation_id = self.prepared_and_attached()

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete(
                operation_id,
                {"candidates": [valid_candidate()] * 21},
            )

    def test_extraction_rejects_candidate_over_sixteen_kibibytes(self) -> None:
        operation_id = self.prepared_and_attached()
        oversized = valid_candidate(claim="x" * (16 * 1024))

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete(operation_id, {"candidates": [oversized]})

    def test_validation_finishes_before_any_candidate_is_written(self) -> None:
        operation_id = self.prepared_and_attached()
        invalid = valid_candidate()
        invalid["claim"] = ""

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete(
                operation_id,
                {"candidates": [valid_candidate(), invalid]},
            )

        self.assertFalse((self.state_dir / "candidates").exists())


if __name__ == "__main__":
    unittest.main()
