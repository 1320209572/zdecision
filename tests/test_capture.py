from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_inventory import VALID_INVENTORY


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"


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
                LegacyCaptureRecord,
                SourceCheckpoint,
                StageFailure,
            )
            from zdecision.capture.inventory import validate_inventory
            from zdecision.capture.templates import TemplateCatalog
            from zdecision.ids import capture_operation_id
            from zdecision.jsonio import atomic_write_json, canonical_json_bytes
            from zdecision.private_store.filesystem import (
                FilePrivateStore,
                InvalidPrivateObjectId,
                PrivateStateConflict,
                PrivateStateCorrupt,
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Capture model API is missing: {exc}")

        return SimpleNamespace(
            CaptureRecord=CaptureRecord,
            LegacyCaptureRecord=LegacyCaptureRecord,
            SourceCheckpoint=SourceCheckpoint,
            StageFailure=StageFailure,
            Candidate=Candidate,
            CandidateContent=CandidateContent,
            TemplateCatalog=TemplateCatalog,
            validate_inventory=validate_inventory,
            capture_operation_id=capture_operation_id,
            atomic_write_json=atomic_write_json,
            canonical_json_bytes=canonical_json_bytes,
            FilePrivateStore=FilePrivateStore,
            InvalidPrivateObjectId=InvalidPrivateObjectId,
            PrivateStateConflict=PrivateStateConflict,
            PrivateStateCorrupt=PrivateStateCorrupt,
        )

    def snapshot(self, api: SimpleNamespace):
        return api.TemplateCatalog(TEMPLATE_ROOT, ENVELOPE_ROOT).render(
            "business", "anheng"
        )

    def test_operation_id_is_stable_and_sensitive_to_every_v2_input(self) -> None:
        """Catch random ids or ids that omit a V2 checkpoint/template input."""
        api = self.capture_api()
        snapshot = self.snapshot(api)

        first = api.capture_operation_id("thread-a", "turn-7", "anheng", snapshot)

        self.assertEqual(
            first,
            api.capture_operation_id("thread-a", "turn-7", "anheng", snapshot),
        )
        self.assertNotEqual(
            first,
            api.capture_operation_id("thread-b", "turn-7", "anheng", snapshot),
        )
        self.assertNotEqual(
            first,
            api.capture_operation_id("thread-a", "turn-8", "anheng", snapshot),
        )
        self.assertNotEqual(
            first,
            api.capture_operation_id(
                "thread-a", "turn-7", "other-product", snapshot
            ),
        )
        template_changes = (
            replace(snapshot, template_id="architecture"),
            replace(snapshot, revision=snapshot.revision + 1),
            replace(snapshot, template_source_sha256="a" * 64),
            replace(snapshot, prompt_bundle_sha256="b" * 64),
        )
        for changed in template_changes:
            with self.subTest(changed=changed):
                self.assertNotEqual(
                    first,
                    api.capture_operation_id(
                        "thread-a", "turn-7", "anheng", changed
                    ),
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

    def test_private_store_round_trips_completed_v2_capture_and_frozen_prompts(
        self,
    ) -> None:
        """Catch lost stage metadata or accidental model/source persistence."""
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        snapshot = self.snapshot(api)
        operation_id = api.capture_operation_id(
            "thread-a", "turn-7", "anheng", snapshot
        )
        record = api.CaptureRecord.started(
            operation_id=operation_id,
            source=api.SourceCheckpoint("thread-a", "turn-7"),
            product="anheng",
            template=snapshot,
        )
        completed = replace(
            record,
            status="completed",
            fork_thread_id="thread-fork",
            inventory_turn_id="turn-inventory",
            extraction_turn_id="turn-extraction",
            inventory_sha256="1" * 64,
            extraction_sha256="2" * 64,
            candidate_ids=(f"cand_{operation_id[4:]}_01",),
        )

        store.put_capture(completed)
        loaded = store.get_capture(operation_id)

        self.assertEqual(completed, loaded)
        self.assertEqual(snapshot.inventory_prompt, loaded.template.inventory_prompt)
        self.assertEqual(snapshot.extraction_prompt, loaded.template.extraction_prompt)
        self.assertEqual("1" * 64, loaded.inventory_sha256)
        self.assertEqual("2" * 64, loaded.extraction_sha256)
        serialized = (
            self.state_dir / "captures" / f"{operation_id}.json"
        ).read_text("utf-8")
        self.assertNotIn("raw_messages", serialized)
        self.assertNotIn("transcript", serialized)
        self.assertNotIn("model_payload", serialized)
        public = loaded.public_dict()
        self.assertNotIn("inventory_prompt", json.dumps(public))
        self.assertNotIn("extraction_prompt", json.dumps(public))

    def test_private_store_round_trips_sanitized_failure_metadata(self) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        snapshot = self.snapshot(api)
        operation_id = api.capture_operation_id(
            "thread-a", "turn-7", "anheng", snapshot
        )
        record = api.CaptureRecord.started(
            operation_id=operation_id,
            source=api.SourceCheckpoint("thread-a", "turn-7"),
            product="anheng",
            template=snapshot,
        )
        failed = replace(
            record,
            status="failed",
            fork_thread_id="thread-fork",
            inventory_turn_id="turn-inventory",
            failure=api.StageFailure(
                stage="inventory",
                code="invalid_inventory",
                message="Inventory output does not match the required schema",
                output_sha256="3" * 64,
            ),
        )

        store.put_capture(failed)

        self.assertEqual(failed, store.get_capture(operation_id))
        serialized = (
            self.state_dir / "captures" / f"{operation_id}.json"
        ).read_text("utf-8")
        self.assertNotIn("raw_messages", serialized)
        self.assertNotIn("transcript", serialized)
        self.assertNotIn("model_payload", serialized)

    def test_payload_validation_failures_require_a_digest_on_construction_and_load(
        self,
    ) -> None:
        """Catch typed invalid payload failures being persisted without provenance."""
        api = self.capture_api()
        cases = (
            ("inventory", "invalid_json", "Stage output was not valid JSON"),
            (
                "inventory",
                "invalid_inventory",
                "Inventory output does not match the required schema",
            ),
            (
                "inventory",
                "inventory_signal_limit_exceeded",
                "Inventory contains more than 100 signals",
            ),
            (
                "inventory",
                "inventory_output_too_large",
                "Inventory output exceeds 256 KiB",
            ),
            ("extraction", "invalid_json", "Stage output was not valid JSON"),
            (
                "extraction",
                "invalid_extraction",
                "Extraction output does not match the required schema",
            ),
            (
                "extraction",
                "candidate_limit_exceeded",
                "Extraction contains more than 20 Candidates",
            ),
            (
                "extraction",
                "candidate_item_too_large",
                "A Candidate exceeds 16 KiB",
            ),
        )

        for stage, code, message in cases:
            with self.subTest(stage=stage, code=code, boundary="construction"):
                with self.assertRaises(ValueError):
                    api.StageFailure(stage, code, message, None)
            with self.subTest(stage=stage, code=code, boundary="load"):
                with self.assertRaises(ValueError):
                    api.StageFailure.from_dict(
                        {
                            "stage": stage,
                            "code": code,
                            "message": message,
                            "output_sha256": None,
                        }
                    )

    def test_v2_capture_loading_rejects_unsanitized_failure_combinations(
        self,
    ) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        snapshot = self.snapshot(api)
        operation_id = api.capture_operation_id(
            "thread-a", "turn-failure-tamper", "anheng", snapshot
        )
        failed = replace(
            api.CaptureRecord.started(
                operation_id,
                api.SourceCheckpoint("thread-a", "turn-failure-tamper"),
                "anheng",
                snapshot,
            ),
            status="failed",
            fork_thread_id="thread-fork",
            inventory_turn_id="turn-inventory",
            failure=api.StageFailure(
                stage="inventory",
                code="invalid_inventory",
                message="Inventory output does not match the required schema",
                output_sha256="3" * 64,
            ),
        )
        invalid_failures = (
            {
                "stage": "inventory",
                "code": "MODEL_SECRET_CODE",
                "message": "MODEL_SECRET_MESSAGE",
                "output_sha256": "3" * 64,
            },
            {
                "stage": "inventory",
                "code": "invalid_inventory",
                "message": "MODEL_SECRET_MESSAGE",
                "output_sha256": "3" * 64,
            },
            {
                "stage": "inventory",
                "code": "invalid_extraction",
                "message": "Extraction output does not match the required schema",
                "output_sha256": "3" * 64,
            },
            {
                "stage": "inventory",
                "code": "invalid_json",
                "message": "Stage output was not valid JSON",
                "output_sha256": None,
            },
        )

        for failure in invalid_failures:
            with self.subTest(failure=failure):
                payload = failed.to_dict()
                payload["failure"] = failure
                api.atomic_write_json(
                    self.state_dir / "captures" / f"{operation_id}.json",
                    payload,
                )

                with self.assertRaises(api.PrivateStateCorrupt):
                    store.get_capture(operation_id)

    def test_capture_record_rejects_unknown_persisted_fields(self) -> None:
        """Catch silent acceptance of unowned V2 state."""
        api = self.capture_api()
        snapshot = self.snapshot(api)
        operation_id = api.capture_operation_id(
            "thread-a", "turn-7", "anheng", snapshot
        )
        record = api.CaptureRecord.started(
            operation_id=operation_id,
            source=api.SourceCheckpoint("thread-a", "turn-7"),
            product="anheng",
            template=snapshot,
        )
        payload = record.to_dict()
        payload["legacy_state"] = "must not load"

        with self.assertRaises(ValueError):
            api.CaptureRecord.from_dict(payload)

    def test_v2_capture_loading_rejects_identity_input_tampering(self) -> None:
        api = self.capture_api()
        snapshot = self.snapshot(api)
        operation_id = api.capture_operation_id(
            "thread-a", "turn-7", "anheng", snapshot
        )
        record = api.CaptureRecord.started(
            operation_id=operation_id,
            source=api.SourceCheckpoint("thread-a", "turn-7"),
            product="anheng",
            template=snapshot,
        )

        for field, changed in (
            ("product", "different-product"),
            ("operation_id", "cap_" + "f" * 32),
        ):
            with self.subTest(field=field):
                payload = record.to_dict()
                payload[field] = changed
                with self.assertRaises(ValueError):
                    api.CaptureRecord.from_dict(payload)

    def test_get_capture_rejects_a_different_internally_valid_object_id(self) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        snapshot = self.snapshot(api)
        requested_id = api.capture_operation_id(
            "thread-a", "turn-7", "anheng", snapshot
        )
        other_id = api.capture_operation_id(
            "thread-b", "turn-7", "anheng", snapshot
        )
        other = api.CaptureRecord.started(
            operation_id=other_id,
            source=api.SourceCheckpoint("thread-b", "turn-7"),
            product="anheng",
            template=snapshot,
        )
        api.atomic_write_json(
            self.state_dir / "captures" / f"{requested_id}.json",
            other.to_dict(),
        )

        with self.assertRaises(api.PrivateStateCorrupt) as raised:
            store.get_capture(requested_id)
        self.assertEqual("captures", raised.exception.collection)
        self.assertEqual(requested_id, raised.exception.object_id)

    def test_failed_capture_shape_requires_a_turn_except_for_native_unavailable(
        self,
    ) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        snapshot = self.snapshot(api)

        cases = (
            (
                "inventory",
                {"inventory_turn_id": None, "inventory_sha256": None},
            ),
            (
                "extraction",
                {
                    "inventory_turn_id": "turn-inventory",
                    "inventory_sha256": "1" * 64,
                    "extraction_turn_id": None,
                },
            ),
        )
        for index, (stage, fields) in enumerate(cases, start=1):
            with self.subTest(stage=stage):
                turn_id = f"turn-impossible-{index}"
                operation_id = api.capture_operation_id(
                    "thread-a", turn_id, "anheng", snapshot
                )
                started = api.CaptureRecord.started(
                    operation_id,
                    api.SourceCheckpoint("thread-a", turn_id),
                    "anheng",
                    snapshot,
                )
                impossible = replace(
                    started,
                    status="failed",
                    fork_thread_id="thread-fork",
                    failure=api.StageFailure(
                        stage=stage,
                        code=f"invalid_{stage}",
                        message=(
                            "Inventory output does not match the required schema"
                            if stage == "inventory"
                            else "Extraction output does not match the required schema"
                        ),
                        output_sha256="2" * 64,
                    ),
                    **fields,
                )
                api.atomic_write_json(
                    self.state_dir / "captures" / f"{operation_id}.json",
                    impossible.to_dict(),
                )

                with self.assertRaises(api.PrivateStateCorrupt):
                    store.get_capture(operation_id)

        native_turn_id = "turn-native-unavailable"
        native_operation_id = api.capture_operation_id(
            "thread-a", native_turn_id, "anheng", snapshot
        )
        native = replace(
            api.CaptureRecord.started(
                native_operation_id,
                api.SourceCheckpoint("thread-a", native_turn_id),
                "anheng",
                snapshot,
            ),
            status="failed",
            fork_thread_id="thread-fork",
            failure=api.StageFailure(
                stage="inventory",
                code="native_unavailable",
                message="Required native task capability was unavailable",
                output_sha256=None,
            ),
        )
        store.put_capture(native)
        self.assertEqual(native, store.get_capture(native_operation_id))

    def test_extractor_v1_capture_loads_as_read_only_legacy_record(self) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        operation_id = "cap_" + "a" * 32
        api.atomic_write_json(
            self.state_dir / "captures" / f"{operation_id}.json",
            {
                "operation_id": operation_id,
                "source": {"thread_id": "thread-old", "turn_id": "turn-old"},
                "product": "anheng",
                "status": "completed",
                "fork_thread_id": "thread-old-fork",
                "candidate_ids": ["cand_old_01"],
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-01T00:01:00Z",
            },
        )

        loaded = store.get_capture(operation_id)

        self.assertIsInstance(loaded, api.LegacyCaptureRecord)
        self.assertEqual(1, loaded.record_version)
        self.assertEqual(("cand_old_01",), loaded.candidate_ids)

    def test_inventory_store_is_idempotent_but_never_replaces_content(self) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        operation_id = "cap_" + "a" * 32
        inventory = api.validate_inventory(
            {
                "signals": [],
                "coverage": {
                    "reviewed_retained_context": "earliest_to_latest",
                    "known_gaps": ["authorization boundary is absent"],
                },
            }
        )

        store.put_inventory(operation_id, inventory)
        store.put_inventory(operation_id, inventory)

        self.assertEqual(inventory, store.get_inventory(operation_id))
        changed = api.validate_inventory(
            {
                "signals": [],
                "coverage": {
                    "reviewed_retained_context": "earliest_to_latest",
                    "known_gaps": [],
                },
            }
        )
        with self.assertRaises(api.PrivateStateConflict):
            store.put_inventory(operation_id, changed)

    def test_candidate_store_is_idempotent_but_never_replaces_content(self) -> None:
        """Catch crash recovery replacing an already-owned Candidate ordinal."""
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        candidate_id = "cand_" + "a" * 32 + "_01"
        candidate = api.Candidate(
            candidate_id=candidate_id,
            capture_id="cap_" + "a" * 32,
            ordinal=1,
            content=api.CandidateContent(
                product="anheng",
                claim="Keep the original Candidate bytes.",
                future_action="Reject a different retry payload.",
                scope_summary="private state",
                repositories=(),
                paths=(),
                invalidation_conditions=(),
            ),
            source=api.SourceCheckpoint("thread-a", "turn-a"),
        )
        path = self.state_dir / "candidates" / f"{candidate_id}.json"

        store.put_candidate(candidate)
        original_bytes = path.read_bytes()
        original_inode = path.stat().st_ino
        store.put_candidate(candidate)

        self.assertEqual(original_bytes, path.read_bytes())
        self.assertEqual(original_inode, path.stat().st_ino)
        changed = replace(
            candidate,
            content=replace(
                candidate.content,
                claim="A different Candidate must never replace the original.",
            ),
        )
        with self.assertRaises(api.PrivateStateConflict):
            store.put_candidate(changed)
        self.assertEqual(original_bytes, path.read_bytes())
        self.assertEqual(original_inode, path.stat().st_ino)

    def test_candidate_store_rejects_different_bytes_for_the_same_typed_value(
        self,
    ) -> None:
        """Catch non-canonical bytes being accepted as an identical Candidate."""
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        candidate_id = "cand_" + "b" * 32 + "_01"
        candidate = api.Candidate(
            candidate_id=candidate_id,
            capture_id="cap_" + "b" * 32,
            ordinal=1,
            content=api.CandidateContent(
                product="anheng",
                claim="Bind idempotency to exact persisted bytes.",
                future_action="Reject any byte-level difference.",
                scope_summary="private state",
                repositories=(),
                paths=(),
                invalidation_conditions=(),
            ),
            source=api.SourceCheckpoint("thread-b", "turn-b"),
        )
        path = self.state_dir / "candidates" / f"{candidate_id}.json"
        store.put_candidate(candidate)
        path.write_text(json.dumps(candidate.to_dict()), "utf-8")
        different_bytes = path.read_bytes()

        with self.assertRaises(api.PrivateStateConflict):
            store.put_candidate(candidate)
        self.assertEqual(different_bytes, path.read_bytes())

    def test_corrupt_private_objects_raise_only_the_sanitized_boundary(self) -> None:
        api = self.capture_api()
        secret = "PRIVATE_STATE_SECRET_7102"
        cases = (
            (
                "captures",
                "cap_corrupt",
                lambda store, object_id: store.get_capture(object_id),
            ),
            (
                "inventories",
                "cap_corrupt",
                lambda store, object_id: store.get_inventory(object_id),
            ),
            (
                "candidates",
                "cand_corrupt",
                lambda store, object_id: store.get_candidate(object_id),
            ),
        )

        for collection, object_id, reader in cases:
            for suffix, raw in (
                ("malformed", ("{\"secret\":\"" + secret).encode("utf-8")),
                ("typed", json.dumps({"secret": secret}).encode("utf-8")),
            ):
                with self.subTest(collection=collection, suffix=suffix):
                    state_dir = self.state_dir / suffix / collection
                    store = api.FilePrivateStore(state_dir)
                    path = state_dir / collection / f"{object_id}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)

                    with self.assertRaises(api.PrivateStateCorrupt) as raised:
                        reader(store, object_id)
                    self.assertEqual(collection, raised.exception.collection)
                    self.assertEqual(object_id, raised.exception.object_id)
                    self.assertNotIn(secret, str(raised.exception))
                    self.assertNotIn("JSONDecodeError", str(raised.exception))
                    self.assertNotIn("ValueError", str(raised.exception))

    def test_invalid_utf8_private_state_is_sanitized(self) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        operation_id = "cap_invalid_utf8"
        path = self.state_dir / "captures" / f"{operation_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfePRIVATE")

        with self.assertRaises(api.PrivateStateCorrupt):
            store.get_capture(operation_id)

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

    def test_candidate_reader_rejects_a_different_internal_object_id(self) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        requested_id = "cand_" + "a" * 32 + "_01"
        other_id = "cand_" + "b" * 32 + "_01"
        candidate = api.Candidate(
            candidate_id=other_id,
            capture_id="cap_" + "b" * 32,
            ordinal=1,
            content=api.CandidateContent(
                product="anheng",
                claim="Keep object identity stable.",
                future_action="Reject swapped private objects.",
                scope_summary="private state",
                repositories=(),
                paths=(),
                invalidation_conditions=(),
            ),
            source=api.SourceCheckpoint("thread-b", "turn-b"),
        )
        api.atomic_write_json(
            self.state_dir / "candidates" / f"{requested_id}.json",
            candidate.to_dict(),
        )

        with self.assertRaises(api.PrivateStateCorrupt) as raised:
            store.get_candidate(requested_id)
        self.assertEqual("candidates", raised.exception.collection)
        self.assertEqual(requested_id, raised.exception.object_id)

    def test_candidate_reader_rejects_empty_fields_and_nonpositive_ordinal(
        self,
    ) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        candidate_id = "cand_" + "a" * 32 + "_01"
        candidate = api.Candidate(
            candidate_id=candidate_id,
            capture_id="cap_" + "a" * 32,
            ordinal=1,
            content=api.CandidateContent(
                product="anheng",
                claim="Keep object fields non-empty.",
                future_action="Reject corrupt persisted Candidates.",
                scope_summary="private state",
                repositories=("https://example.com/zdecision.git",),
                paths=("decision-registry/",),
                invalidation_conditions=("The contract changes.",),
            ),
            source=api.SourceCheckpoint("thread-a", "turn-a"),
        )
        invalid_paths = (
            (("capture_id",), ""),
            (("ordinal",), -7),
            (("content", "claim"), ""),
            (("source", "turn_id"), ""),
        )

        for field_path, invalid_value in invalid_paths:
            with self.subTest(field_path=field_path):
                payload = json.loads(json.dumps(candidate.to_dict()))
                target = payload
                for field in field_path[:-1]:
                    target = target[field]
                target[field_path[-1]] = invalid_value
                api.atomic_write_json(
                    self.state_dir / "candidates" / f"{candidate_id}.json",
                    payload,
                )

                with self.assertRaises(api.PrivateStateCorrupt):
                    store.get_candidate(candidate_id)

    def test_v2_completed_capture_rejects_invalid_candidate_reference_sets(
        self,
    ) -> None:
        api = self.capture_api()
        store = api.FilePrivateStore(self.state_dir)
        snapshot = self.snapshot(api)
        operation_id = api.capture_operation_id(
            "thread-a", "turn-reference-tamper", "anheng", snapshot
        )
        completed = replace(
            api.CaptureRecord.started(
                operation_id,
                api.SourceCheckpoint("thread-a", "turn-reference-tamper"),
                "anheng",
                snapshot,
            ),
            status="completed",
            fork_thread_id="thread-fork",
            inventory_turn_id="turn-inventory",
            extraction_turn_id="turn-extraction",
            inventory_sha256="1" * 64,
            extraction_sha256="2" * 64,
        )
        prefix = f"cand_{operation_id[4:]}_"
        invalid_sets = (
            ("cand_old_01",),
            (f"{prefix}02", f"{prefix}01"),
            (f"{prefix}01", f"{prefix}01"),
            tuple(f"{prefix}{ordinal:02d}" for ordinal in range(1, 22)),
        )

        for candidate_ids in invalid_sets:
            with self.subTest(candidate_ids=candidate_ids):
                payload = replace(completed, candidate_ids=candidate_ids).to_dict()
                api.atomic_write_json(
                    self.state_dir / "captures" / f"{operation_id}.json",
                    payload,
                )
                with self.assertRaises(api.PrivateStateCorrupt):
                    store.get_capture(operation_id)

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
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.template_root = self.root / "decision-templates"
        shutil.copytree(TEMPLATE_ROOT, self.template_root)
        self.operation_counter = 0

        from zdecision.capture.service import (
            CaptureForkAmbiguous,
            CaptureForkConflict,
            CaptureService,
            CaptureStateError,
            CaptureTurnConflict,
            ExtractionValidationError,
            validate_extraction_output,
        )
        from zdecision.capture.templates import TemplateCatalog
        from zdecision.private_store.filesystem import (
            FilePrivateStore,
            PrivateStateCorrupt,
        )

        self.CaptureForkAmbiguous = CaptureForkAmbiguous
        self.CaptureForkConflict = CaptureForkConflict
        self.CaptureStateError = CaptureStateError
        self.CaptureTurnConflict = CaptureTurnConflict
        self.ExtractionValidationError = ExtractionValidationError
        self.validate_extraction_output = validate_extraction_output
        self.PrivateStateCorrupt = PrivateStateCorrupt
        self.store = FilePrivateStore(self.state_dir)
        self.catalog = TemplateCatalog(self.template_root, ENVELOPE_ROOT)
        self.service = CaptureService(self.store, self.catalog)

    def prepare(
        self,
        *,
        product: str = "anheng",
        template_id: str = "business",
        source_turn_id: str | None = None,
    ):
        self.operation_counter += 1
        turn_id = source_turn_id or f"turn-{self.operation_counter}"
        return self.service.prepare("thread-a", turn_id, product, template_id)

    def prepared_and_attached(self) -> str:
        plan = self.prepare()
        self.service.attach_fork(plan.record.operation_id, "thread-fork")
        return plan.record.operation_id

    def inventory_running(self) -> str:
        operation_id = self.prepared_and_attached()
        self.service.attach_stage_turn(
            operation_id, "inventory", "turn-inventory"
        )
        return operation_id

    def inventory_completed(self) -> str:
        operation_id = self.inventory_running()
        self.service.complete_inventory(operation_id, VALID_INVENTORY)
        return operation_id

    def extraction_running(self) -> str:
        operation_id = self.inventory_completed()
        self.service.attach_stage_turn(
            operation_id, "extraction", "turn-extraction"
        )
        return operation_id

    def completed(self) -> str:
        operation_id = self.extraction_running()
        self.service.complete_extraction(operation_id, {"candidates": []})
        return operation_id

    def test_prepare_freezes_both_exact_template_prompts_before_fork(self) -> None:
        expected = self.catalog.render("business", "安恒")

        plan = self.service.prepare(
            "thread-a", "turn-7", "安恒", "business"
        )

        self.assertEqual("prepared", plan.record.status)
        self.assertFalse(plan.replayed)
        self.assertEqual(expected, plan.record.template)
        self.assertEqual(expected.inventory_prompt, plan.inventory_prompt)
        self.assertEqual(expected.extraction_prompt, plan.extraction_prompt)
        self.assertEqual(plan.record, self.store.get_capture(plan.record.operation_id))
        self.assertNotIn("source_text", plan.record.to_dict())

    def test_extracted_validator_preserves_legacy_v2_candidate_identity(
        self,
    ) -> None:
        plan = self.prepare()
        extraction = extraction_with_two_candidates()

        candidates = self.validate_extraction_output(
            plan.record.operation_id,
            plan.record.source,
            plan.record.product,
            extraction,
        )

        self.assertEqual(
            (
                f"cand_{plan.record.operation_id[4:]}_01",
                f"cand_{plan.record.operation_id[4:]}_02",
            ),
            tuple(candidate.candidate_id for candidate in candidates),
        )

    def test_resume_uses_frozen_prompts_after_live_template_changes(self) -> None:
        plan = self.service.prepare(
            "thread-a", "turn-7", "安恒", "business"
        )
        self.service.attach_fork(plan.record.operation_id, "thread-fork")
        policy = self.template_root / "business" / "inventory.md"
        policy.write_text(policy.read_text("utf-8") + "\n新增政策。\n", "utf-8")

        replay = self.service.resume(plan.record.operation_id)
        changed = self.service.prepare(
            "thread-a", "turn-7", "安恒", "business"
        )

        self.assertEqual(plan.inventory_prompt, replay.inventory_prompt)
        self.assertEqual(plan.extraction_prompt, replay.extraction_prompt)
        self.assertNotEqual(plan.record.operation_id, changed.record.operation_id)
        self.assertNotEqual(plan.inventory_prompt, changed.inventory_prompt)

    def test_prepare_retry_before_fork_attach_is_ambiguous_but_resume_is_safe(
        self,
    ) -> None:
        plan = self.service.prepare("thread-a", "turn-7", "anheng")

        resumed = self.service.resume(plan.record.operation_id)

        self.assertTrue(resumed.replayed)
        self.assertEqual("prepared", resumed.record.status)
        self.assertEqual(plan.inventory_prompt, resumed.inventory_prompt)
        self.assertEqual(plan.extraction_prompt, resumed.extraction_prompt)

        with self.assertRaises(self.CaptureForkAmbiguous):
            self.service.prepare("thread-a", "turn-7", "anheng")

    def test_prepare_replays_attached_fork_from_its_snapshot(self) -> None:
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        attached = self.service.attach_fork(
            plan.record.operation_id,
            "thread-fork",
        )

        replay = self.service.prepare("thread-a", "turn-7", "anheng")

        self.assertTrue(replay.replayed)
        self.assertEqual(attached, replay.record)
        self.assertEqual(plan.inventory_prompt, replay.inventory_prompt)

    def test_fork_and_stage_turn_attachment_are_idempotent_by_external_id(
        self,
    ) -> None:
        plan = self.prepare()
        self.service.attach_fork(plan.record.operation_id, "thread-fork")

        replay = self.service.attach_fork(plan.record.operation_id, "thread-fork")

        self.assertEqual("thread-fork", replay.fork_thread_id)
        with self.assertRaises(self.CaptureForkConflict):
            self.service.attach_fork(plan.record.operation_id, "different-fork")

        inventory = self.service.attach_stage_turn(
            plan.record.operation_id, "inventory", "turn-inventory"
        )
        self.assertEqual(
            inventory,
            self.service.attach_stage_turn(
                plan.record.operation_id, "inventory", "turn-inventory"
            ),
        )
        with self.assertRaises(self.CaptureTurnConflict):
            self.service.attach_stage_turn(
                plan.record.operation_id, "inventory", "turn-other"
            )

        self.service.complete_inventory(plan.record.operation_id, VALID_INVENTORY)
        extraction = self.service.attach_stage_turn(
            plan.record.operation_id, "extraction", "turn-extraction"
        )
        self.assertEqual(
            extraction,
            self.service.attach_stage_turn(
                plan.record.operation_id, "extraction", "turn-extraction"
            ),
        )
        with self.assertRaises(self.CaptureTurnConflict):
            self.service.attach_stage_turn(
                plan.record.operation_id, "extraction", "turn-other"
            )

    def test_successful_two_stage_sequence_records_both_digests(self) -> None:
        plan = self.prepare(source_turn_id="turn-7")
        operation_id = plan.record.operation_id
        self.service.attach_fork(operation_id, "thread-fork")
        self.service.attach_stage_turn(operation_id, "inventory", "turn-inventory")
        inventory_record = self.service.complete_inventory(
            operation_id, VALID_INVENTORY
        )
        self.service.attach_stage_turn(operation_id, "extraction", "turn-extract")
        result = self.service.complete_extraction(operation_id, {"candidates": []})

        completed = self.service.get(operation_id)
        self.assertEqual("inventory_completed", inventory_record.status)
        self.assertEqual("completed", result.status)
        self.assertEqual(result.extraction_sha256, completed.extraction_sha256)
        self.assertIsNotNone(completed.inventory_sha256)
        self.assertEqual("turn-inventory", completed.inventory_turn_id)
        self.assertEqual("turn-extract", completed.extraction_turn_id)

    def test_stage_order_is_enforced(self) -> None:
        plan = self.prepare()
        operation_id = plan.record.operation_id
        with self.assertRaises(self.CaptureStateError):
            self.service.attach_stage_turn(
                operation_id, "inventory", "turn-inventory"
            )

        self.service.attach_fork(operation_id, "thread-fork")
        with self.assertRaises(self.CaptureStateError):
            self.service.attach_stage_turn(
                operation_id, "extraction", "turn-extraction"
            )
        with self.assertRaises(self.CaptureStateError):
            self.service.complete_inventory(operation_id, VALID_INVENTORY)

        self.service.attach_stage_turn(operation_id, "inventory", "turn-inventory")
        with self.assertRaises(self.CaptureStateError):
            self.service.complete_extraction(operation_id, {"candidates": []})

    def test_inventory_completion_retry_replays_without_rewriting_artifact(self) -> None:
        operation_id = self.inventory_running()
        first = self.service.complete_inventory(operation_id, VALID_INVENTORY)
        path = self.state_dir / "inventories" / f"{operation_id}.json"
        inode = path.stat().st_ino
        changed = {
            "signals": [],
            "coverage": {
                "reviewed_retained_context": "earliest_to_latest",
                "known_gaps": ["different retry payload"],
            },
        }

        replay = self.service.complete_inventory(operation_id, changed)

        self.assertEqual(first.inventory_sha256, replay.inventory_sha256)
        self.assertEqual(inode, path.stat().st_ino)
        self.assertEqual(VALID_INVENTORY, self.store.get_inventory(operation_id).to_dict())

    def test_inventory_crash_window_reconciles_only_identical_artifact(self) -> None:
        from zdecision.capture.inventory import validate_inventory

        operation_id = self.inventory_running()
        inventory = validate_inventory(VALID_INVENTORY)
        self.store.put_inventory(operation_id, inventory)

        completed = self.service.complete_inventory(operation_id, VALID_INVENTORY)

        self.assertEqual("inventory_completed", completed.status)

        conflicting_operation = self.inventory_running()
        changed = {
            "signals": [],
            "coverage": {
                "reviewed_retained_context": "earliest_to_latest",
                "known_gaps": ["different persisted output"],
            },
        }
        self.store.put_inventory(conflicting_operation, validate_inventory(changed))
        with self.assertRaises(self.CaptureStateError):
            self.service.complete_inventory(conflicting_operation, VALID_INVENTORY)
        self.assertEqual(
            "inventory_running", self.service.get(conflicting_operation).status
        )

    def test_invalid_inventory_records_sanitized_terminal_failure(self) -> None:
        operation_id = self.inventory_running()
        invalid = {"signals": [], "coverage": {}}

        with self.assertRaises(self.service_inventory_error()) as raised:
            self.service.complete_inventory(operation_id, invalid)

        record = self.service.get(operation_id)
        self.assertEqual("failed", record.status)
        self.assertEqual("inventory", record.failure.stage)
        self.assertEqual("invalid_inventory", record.failure.code)
        self.assertIsNotNone(record.failure.output_sha256)
        self.assertFalse((self.state_dir / "inventories").exists())
        with self.assertRaises(self.CaptureStateError):
            self.service.attach_stage_turn(
                operation_id, "extraction", "turn-extraction"
            )
        self.assertEqual("invalid_inventory", raised.exception.code)

    @staticmethod
    def service_inventory_error():
        from zdecision.capture.inventory import InventoryValidationError

        return InventoryValidationError

    def test_inventory_validation_never_echoes_model_authored_secrets(self) -> None:
        secret = "INVENTORY_MODEL_SECRET_3b01"
        cases = (
            {**VALID_INVENTORY, secret: "unknown"},
            {
                **VALID_INVENTORY,
                "signals": [{**VALID_INVENTORY["signals"][0], "status": secret}],
            },
        )

        for invalid in cases:
            with self.subTest(invalid=invalid):
                operation_id = self.inventory_running()
                with self.assertRaises(self.service_inventory_error()) as raised:
                    self.service.complete_inventory(operation_id, invalid)
                record = self.service.get(operation_id)
                self.assertNotIn(secret, raised.exception.message)
                self.assertNotIn(secret, record.failure.message)
                serialized = json.dumps(record.to_dict(), ensure_ascii=False)
                self.assertNotIn(secret, serialized)

    def test_failure_recording_requires_the_eligible_stage_and_is_idempotent(
        self,
    ) -> None:
        operation_id = self.prepared_and_attached()
        with self.assertRaises(self.CaptureStateError):
            self.service.record_invalid_json(
                operation_id, "inventory", "1" * 64
            )

        failed = self.service.record_stage_failure(
            operation_id, "inventory", "native_unavailable"
        )
        replay = self.service.record_stage_failure(
            operation_id, "inventory", "native_unavailable"
        )
        self.assertEqual(failed, replay)
        with self.assertRaises(self.CaptureStateError):
            self.service.record_stage_failure(
                operation_id, "inventory", "model_timeout", "2" * 64
            )

        running = self.inventory_running()
        invalid_json = self.service.record_invalid_json(
            running, "inventory", "3" * 64
        )
        self.assertEqual("invalid_json", invalid_json.failure.code)
        self.assertEqual("3" * 64, invalid_json.failure.output_sha256)

    def test_failed_capture_replays_but_never_restarts(self) -> None:
        plan = self.prepare(source_turn_id="turn-failed")
        operation_id = plan.record.operation_id
        self.service.attach_fork(operation_id, "thread-fork")
        self.service.attach_stage_turn(operation_id, "inventory", "turn-inventory")
        self.service.record_stage_failure(
            operation_id, "inventory", "model_timeout", "4" * 64
        )

        replay = self.service.prepare(
            "thread-a", "turn-failed", "anheng", "business"
        )

        self.assertTrue(replay.replayed)
        self.assertEqual("failed", replay.record.status)
        with self.assertRaises(self.CaptureStateError):
            self.service.complete_inventory(operation_id, VALID_INVENTORY)
        with self.assertRaises(self.CaptureStateError):
            self.service.attach_stage_turn(
                operation_id, "extraction", "turn-extraction"
            )

    def test_required_inventory_is_checked_for_every_later_replay_state(self) -> None:
        for state in (
            "inventory_completed",
            "extraction_running",
            "completed",
            "failed",
        ):
            for corruption in ("missing", "digest_mismatch"):
                with self.subTest(state=state, corruption=corruption):
                    operation_id = self.inventory_completed()
                    if state in ("extraction_running", "completed", "failed"):
                        self.service.attach_stage_turn(
                            operation_id, "extraction", "turn-extraction"
                        )
                    if state == "completed":
                        self.service.complete_extraction(
                            operation_id, {"candidates": []}
                        )
                    elif state == "failed":
                        self.service.record_stage_failure(
                            operation_id,
                            "extraction",
                            "model_timeout",
                            "5" * 64,
                        )
                    record = self.service.get(operation_id)
                    path = self.state_dir / "inventories" / f"{operation_id}.json"
                    if corruption == "missing":
                        path.unlink()
                    else:
                        changed = {
                            "signals": [],
                            "coverage": {
                                "reviewed_retained_context": "earliest_to_latest",
                                "known_gaps": ["changed"],
                            },
                        }
                        from zdecision.jsonio import atomic_write_json

                        atomic_write_json(path, changed)

                    with self.assertRaises(self.CaptureStateError):
                        self.service.resume(operation_id)
                    with self.assertRaises(self.CaptureStateError):
                        self.service.prepare(
                            record.source.thread_id,
                            record.source.turn_id,
                            record.product,
                            record.template.template_id,
                        )

    def test_malformed_required_inventory_keeps_private_corruption_boundary(self) -> None:
        operation_id = self.inventory_completed()
        self.service.attach_stage_turn(
            operation_id, "extraction", "turn-extraction"
        )
        path = self.state_dir / "inventories" / f"{operation_id}.json"
        path.write_text('{"PRIVATE_SECRET":', "utf-8")

        with self.assertRaises(self.PrivateStateCorrupt):
            self.service.resume(operation_id)
        with self.assertRaises(self.PrivateStateCorrupt):
            self.service.complete_extraction(operation_id, {"candidates": []})

    def test_zero_candidates_is_a_completed_result_and_replays(self) -> None:
        operation_id = self.extraction_running()

        result = self.service.complete_extraction(operation_id, {"candidates": []})
        replay = self.service.resume(operation_id)

        self.assertEqual("completed", result.status)
        self.assertEqual((), result.candidate_ids)
        self.assertEqual(result.operation_id, replay.record.operation_id)
        self.assertTrue(replay.replayed)

    def test_complete_retry_returns_stored_result_without_new_candidates(self) -> None:
        operation_id = self.extraction_running()
        completed = self.service.complete_extraction(
            operation_id,
            extraction_with_two_candidates(),
        )

        replay = self.service.complete_extraction(operation_id, {"candidates": []})

        self.assertEqual(completed, replay)
        self.assertEqual(2, len(tuple((self.state_dir / "candidates").iterdir())))

    def test_same_retry_recovers_from_manifest_only_crash(self) -> None:
        """Catch Stage 2 writing a Candidate before atomically owning its payload."""
        operation_id = self.extraction_running()
        extraction = extraction_with_two_candidates()

        with patch.object(
            self.store,
            "put_candidate",
            side_effect=OSError("simulated crash before the first Candidate"),
        ):
            with self.assertRaises(OSError):
                self.service.complete_extraction(operation_id, extraction)

        manifest_path = (
            self.state_dir / "extraction_manifests" / f"{operation_id}.json"
        )
        self.assertTrue(manifest_path.is_file())
        self.assertFalse((self.state_dir / "candidates").exists())
        self.assertEqual("extraction_running", self.service.get(operation_id).status)

        completed = self.service.complete_extraction(operation_id, extraction)

        self.assertEqual("completed", completed.status)
        self.assertEqual(2, len(completed.candidate_ids))

    def test_same_retry_recovers_from_partial_candidate_crash(self) -> None:
        """Catch an identical retry rejecting or replacing a partial Candidate set."""
        operation_id = self.extraction_running()
        extraction = extraction_with_two_candidates()
        original_put_candidate = self.store.put_candidate
        write_count = 0

        def crash_after_first(candidate) -> None:
            nonlocal write_count
            if write_count == 1:
                raise OSError("simulated crash after the first Candidate")
            original_put_candidate(candidate)
            write_count += 1

        with patch.object(
            self.store,
            "put_candidate",
            side_effect=crash_after_first,
        ):
            with self.assertRaises(OSError):
                self.service.complete_extraction(operation_id, extraction)

        first_id = f"cand_{operation_id[4:]}_01"
        first_path = self.state_dir / "candidates" / f"{first_id}.json"
        original_bytes = first_path.read_bytes()
        original_inode = first_path.stat().st_ino

        completed = self.service.complete_extraction(operation_id, extraction)

        self.assertEqual("completed", completed.status)
        self.assertEqual(original_bytes, first_path.read_bytes())
        self.assertEqual(original_inode, first_path.stat().st_ino)

    def test_same_retry_recovers_after_all_candidates_precede_capture_record(self) -> None:
        """Catch a final Capture write failure making a valid Stage 2 unrecoverable."""
        operation_id = self.extraction_running()
        extraction = extraction_with_two_candidates()
        original_put_capture = self.store.put_capture

        def crash_on_completed_record(record) -> None:
            if record.status == "completed":
                raise OSError("simulated crash before the completed Capture record")
            original_put_capture(record)

        with patch.object(
            self.store,
            "put_capture",
            side_effect=crash_on_completed_record,
        ):
            with self.assertRaises(OSError):
                self.service.complete_extraction(operation_id, extraction)

        self.assertTrue(
            (
                self.state_dir
                / "extraction_manifests"
                / f"{operation_id}.json"
            ).is_file()
        )
        self.assertEqual(2, len(tuple((self.state_dir / "candidates").iterdir())))
        self.assertEqual("extraction_running", self.service.get(operation_id).status)

        completed = self.service.complete_extraction(operation_id, extraction)

        self.assertEqual("completed", completed.status)

    def test_different_retry_lengths_conflict_without_mutating_partial_state(self) -> None:
        """Catch any changed retry adopting or orphaning deterministic ordinals."""
        operation_id = self.extraction_running()
        original = extraction_with_two_candidates()
        original_put_candidate = self.store.put_candidate
        write_count = 0

        def crash_after_first(candidate) -> None:
            nonlocal write_count
            if write_count == 1:
                raise OSError("simulated crash after the first Candidate")
            original_put_candidate(candidate)
            write_count += 1

        with patch.object(
            self.store,
            "put_candidate",
            side_effect=crash_after_first,
        ):
            with self.assertRaises(OSError):
                self.service.complete_extraction(operation_id, original)

        manifest_path = (
            self.state_dir / "extraction_manifests" / f"{operation_id}.json"
        )
        first_id = f"cand_{operation_id[4:]}_01"
        first_path = self.state_dir / "candidates" / f"{first_id}.json"
        original_manifest = manifest_path.read_bytes()
        original_candidate = first_path.read_bytes()
        original_manifest_inode = manifest_path.stat().st_ino
        original_candidate_inode = first_path.stat().st_ino
        changed_same_length = extraction_with_two_candidates()
        changed_same_length["candidates"][0]["claim"] = (
            "Reject formal decisions on main."
        )
        retries = (
            ("same_length", changed_same_length),
            ("shorter", {"candidates": original["candidates"][:1]}),
            (
                "longer",
                {
                    "candidates": [
                        *original["candidates"],
                        valid_candidate(claim="A third Candidate is a conflict."),
                    ]
                },
            ),
        )

        for name, retry in retries:
            with self.subTest(name=name):
                with self.assertRaises(self.CaptureStateError):
                    self.service.complete_extraction(operation_id, retry)
                self.assertEqual(original_manifest, manifest_path.read_bytes())
                self.assertEqual(original_candidate, first_path.read_bytes())
                self.assertEqual(original_manifest_inode, manifest_path.stat().st_ino)
                self.assertEqual(original_candidate_inode, first_path.stat().st_ino)
                self.assertEqual(
                    [f"{first_id}.json"],
                    sorted(path.name for path in first_path.parent.iterdir()),
                )
                self.assertEqual(
                    "extraction_running", self.service.get(operation_id).status
                )

    def test_completed_replays_verify_candidate_digest_before_returning(self) -> None:
        from zdecision.jsonio import atomic_write_json

        operation_id = self.extraction_running()
        self.service.complete_extraction(
            operation_id,
            {"candidates": [valid_candidate()]},
        )
        record = self.service.get(operation_id)
        candidate_id = record.candidate_ids[0]
        candidate = self.store.get_candidate(candidate_id)
        assert candidate is not None
        payload = candidate.to_dict()
        payload["content"]["claim"] = "Tampered but still valid Candidate text."
        atomic_write_json(
            self.state_dir / "candidates" / f"{candidate_id}.json",
            payload,
        )

        replay_actions = (
            lambda: self.service.resume(operation_id),
            lambda: self.service.prepare(
                record.source.thread_id,
                record.source.turn_id,
                record.product,
                record.template.template_id,
            ),
            lambda: self.service.complete_extraction(
                operation_id, {"candidates": []}
            ),
            lambda: self.service.attach_fork(operation_id, "thread-fork"),
            lambda: self.service.attach_stage_turn(
                operation_id, "inventory", "turn-inventory"
            ),
            lambda: self.service.attach_stage_turn(
                operation_id, "extraction", "turn-extraction"
            ),
            lambda: self.service.complete_inventory(
                operation_id, VALID_INVENTORY
            ),
        )
        for replay in replay_actions:
            with self.subTest(replay=replay):
                with self.assertRaises(self.PrivateStateCorrupt):
                    replay()

    def test_candidate_ids_follow_validated_result_order(self) -> None:
        operation_id = self.extraction_running()

        result = self.service.complete_extraction(
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
        operation_id = self.extraction_running()
        secret = "CANDIDATE_MODEL_SECRET_d1a4"

        with self.assertRaises(self.ExtractionValidationError) as raised:
            self.service.complete_extraction(
                operation_id,
                {
                    "candidates": [
                        {
                            **valid_candidate(),
                            secret: "private source text",
                        }
                    ]
                },
            )

        self.assertFalse((self.state_dir / "candidates").exists())
        record = self.service.get(operation_id)
        self.assertEqual("failed", record.status)
        self.assertEqual("invalid_extraction", raised.exception.code)
        self.assertNotIn(secret, raised.exception.message)
        self.assertNotIn(secret, record.failure.message)
        self.assertIsNotNone(self.store.get_inventory(operation_id))

    def test_candidate_limit_is_checked_before_candidate_shape(self) -> None:
        operation_id = self.extraction_running()

        with self.assertRaises(self.ExtractionValidationError) as raised:
            self.service.complete_extraction(
                operation_id,
                {"candidates": [{}] * 21},
            )

        self.assertEqual("candidate_limit_exceeded", raised.exception.code)
        self.assertEqual(
            "candidate_limit_exceeded", self.service.get(operation_id).failure.code
        )
        self.assertFalse((self.state_dir / "candidates").exists())

    def test_extraction_rejects_empty_required_text(self) -> None:
        for field in ("product", "claim", "future_action"):
            candidate = valid_candidate()
            candidate[field] = " "
            with self.subTest(field=field):
                operation_id = self.extraction_running()
                with self.assertRaises(self.ExtractionValidationError):
                    self.service.complete_extraction(
                        operation_id,
                        {"candidates": [candidate]},
                    )

        candidate = valid_candidate()
        scope = dict(candidate["scope"])
        scope["summary"] = ""
        candidate["scope"] = scope
        operation_id = self.extraction_running()
        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete_extraction(
                operation_id, {"candidates": [candidate]}
            )

    def test_extraction_rejects_a_silently_changed_product(self) -> None:
        operation_id = self.extraction_running()
        candidate = valid_candidate()
        candidate["product"] = "安恒"

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete_extraction(
                operation_id, {"candidates": [candidate]}
            )

        self.assertFalse((self.state_dir / "candidates").exists())

    def test_extraction_rejects_non_string_list_members(self) -> None:
        operation_id = self.extraction_running()
        candidate = valid_candidate()
        scope = dict(candidate["scope"])
        scope["paths"] = ["decision-registry/", 7]
        candidate["scope"] = scope

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete_extraction(
                operation_id, {"candidates": [candidate]}
            )

    def test_extraction_rejects_candidate_over_sixteen_kibibytes(self) -> None:
        operation_id = self.extraction_running()
        oversized = valid_candidate(claim="x" * (16 * 1024))

        with self.assertRaises(self.ExtractionValidationError) as raised:
            self.service.complete_extraction(
                operation_id, {"candidates": [oversized]}
            )
        self.assertEqual("candidate_item_too_large", raised.exception.code)

    def test_validation_finishes_before_any_candidate_is_written(self) -> None:
        operation_id = self.extraction_running()
        invalid = valid_candidate()
        invalid["claim"] = ""

        with self.assertRaises(self.ExtractionValidationError):
            self.service.complete_extraction(
                operation_id,
                {"candidates": [valid_candidate(), invalid]},
            )

        self.assertFalse((self.state_dir / "candidates").exists())

    def test_legacy_record_is_displayable_but_no_service_mutation_accepts_it(self) -> None:
        from zdecision.jsonio import atomic_write_json

        operation_id = "cap_" + "a" * 32
        atomic_write_json(
            self.state_dir / "captures" / f"{operation_id}.json",
            {
                "operation_id": operation_id,
                "source": {"thread_id": "thread-old", "turn_id": "turn-old"},
                "product": "anheng",
                "status": "completed",
                "fork_thread_id": "thread-old-fork",
                "candidate_ids": [],
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-01T00:01:00Z",
            },
        )

        self.assertEqual(1, self.service.get(operation_id).record_version)
        for mutation in (
            lambda: self.service.attach_fork(operation_id, "thread-new"),
            lambda: self.service.attach_stage_turn(
                operation_id, "inventory", "turn-new"
            ),
            lambda: self.service.complete_inventory(operation_id, VALID_INVENTORY),
            lambda: self.service.complete_extraction(
                operation_id, {"candidates": []}
            ),
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(self.CaptureStateError):
                    mutation()


class EvidenceFirstExtractionTests(unittest.TestCase):
    def test_v5_extraction_binds_each_candidate_to_one_eligible_signal(self) -> None:
        """This catches extraction bypassing the validated Inventory sidecar."""
        from tests.test_inventory import evidence_manifest, v5_inventory
        from zdecision.capture.inventory import validate_inventory_v5
        from zdecision.capture.models import SourceCheckpoint
        from zdecision.capture.service import (
            ExtractionValidationError,
            validate_extraction_output_v5,
        )

        manifest = evidence_manifest()
        inventory, signal_provenance = validate_inventory_v5(
            v5_inventory(manifest), manifest
        )
        extraction = {
            "candidates": [
                {
                    **valid_candidate(),
                    "source_signal_ordinal": 1,
                }
            ]
        }

        candidates, candidate_provenance = validate_extraction_output_v5(
            "cap_" + "a" * 32,
            SourceCheckpoint("thread-1", "turn-1"),
            "anheng",
            extraction,
            inventory,
            signal_provenance,
            manifest,
        )

        self.assertEqual(1, len(candidates))
        self.assertEqual(
            (manifest.anchors[0].receipt_id,),
            candidate_provenance[0].evidence_receipt_ids,
        )
        forged = {
            "candidates": [
                {
                    **extraction["candidates"][0],
                    "evidence_receipt_ids": [manifest.anchors[0].receipt_id],
                }
            ]
        }
        with self.assertRaises(ExtractionValidationError):
            validate_extraction_output_v5(
                "cap_" + "a" * 32,
                SourceCheckpoint("thread-1", "turn-1"),
                "anheng",
                forged,
                inventory,
                signal_provenance,
                manifest,
            )

    def test_v5_extraction_rejects_duplicate_or_noneligible_ordinals(self) -> None:
        """This catches Extraction reusing or promoting an Inventory signal."""
        from tests.test_inventory import evidence_manifest, v5_inventory
        from zdecision.capture.inventory import validate_inventory_v5
        from zdecision.capture.models import SourceCheckpoint
        from zdecision.capture.service import (
            ExtractionValidationError,
            validate_extraction_output_v5,
        )

        manifest = evidence_manifest()
        inventory, provenance = validate_inventory_v5(v5_inventory(manifest), manifest)
        duplicated = {
            "candidates": [
                {**valid_candidate(), "source_signal_ordinal": 1},
                {
                    **valid_candidate(claim="another rule"),
                    "source_signal_ordinal": 1,
                },
            ]
        }
        with self.assertRaises(ExtractionValidationError):
            validate_extraction_output_v5(
                "cap_" + "a" * 32,
                SourceCheckpoint("thread-1", "turn-1"),
                "anheng",
                duplicated,
                inventory,
                provenance,
                manifest,
            )

    def test_v5_extraction_preserves_multi_receipt_manifest_order(self) -> None:
        """This catches Candidate sidecars reordering a selected receipt set."""
        from tests.test_inventory import multi_receipt_manifest, v5_inventory
        from zdecision.capture.inventory import validate_inventory_v5
        from zdecision.capture.models import SourceCheckpoint
        from zdecision.capture.service import (
            ExtractionValidationError,
            validate_extraction_output_v5,
        )

        manifest = multi_receipt_manifest()
        value = v5_inventory(manifest)
        value["signals"][0]["evidence_receipt_ids"] = [
            anchor.receipt_id for anchor in manifest.anchors
        ]
        inventory, provenance = validate_inventory_v5(value, manifest)
        _, candidate_provenance = validate_extraction_output_v5(
            "cap_" + "a" * 32,
            SourceCheckpoint("thread-1", "turn-1"),
            "anheng",
            {"candidates": [{**valid_candidate(), "source_signal_ordinal": 1}]},
            inventory,
            provenance,
            manifest,
        )
        self.assertEqual(
            tuple(anchor.receipt_id for anchor in manifest.anchors),
            candidate_provenance[0].evidence_receipt_ids,
        )

        ineligible = v5_inventory(manifest)
        ineligible["signals"][0]["status"] = "unresolved"
        inventory, provenance = validate_inventory_v5(ineligible, manifest)
        with self.assertRaises(ExtractionValidationError):
            validate_extraction_output_v5(
                "cap_" + "a" * 32,
                SourceCheckpoint("thread-1", "turn-1"),
                "anheng",
                {"candidates": [{**valid_candidate(), "source_signal_ordinal": 1}]},
                inventory,
                provenance,
                manifest,
            )


if __name__ == "__main__":
    unittest.main()
