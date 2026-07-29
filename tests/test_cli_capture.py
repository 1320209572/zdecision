from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_inventory import VALID_INVENTORY


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"


def run_cli(
    argv: list[str],
    *,
    state_dir: Path,
    stdin: str = "",
    template_root: Path | None = None,
) -> tuple[int, dict[str, object], str, str]:
    from zdecision.cli import main

    stdout_stream = io.StringIO()
    stderr_stream = io.StringIO()
    environ = {"ZDECISION_STATE_DIR": str(state_dir)}
    if template_root is not None:
        environ["ZDECISION_TEMPLATE_ROOT"] = str(template_root)
    code = main(
        argv,
        io.StringIO(stdin),
        stdout_stream,
        stderr_stream,
        environ,
    )
    stdout = stdout_stream.getvalue()
    return code, json.loads(stdout), stdout, stderr_stream.getvalue()


def valid_candidate() -> dict[str, object]:
    return {
        "product": "anheng",
        "claim": "Keep private state outside Git.",
        "future_action": "Use the user-local private store.",
        "scope": {
            "summary": "ZDecision",
            "repositories": [],
            "paths": [],
        },
        "invalidation_conditions": [],
    }


class CaptureCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.template_root = self.root / "decision-templates"
        shutil.copytree(TEMPLATE_ROOT, self.template_root)
        self.counter = 0

    def run_capture(
        self,
        argv: list[str],
        *,
        stdin: str = "",
        template_root: Path | None = None,
    ) -> tuple[int, dict[str, object], str, str]:
        return run_cli(
            argv,
            state_dir=self.state_dir,
            stdin=stdin,
            template_root=template_root,
        )

    def prepare(
        self,
        *,
        template_id: str = "business",
        template_root: Path | None = None,
    ) -> tuple[str, dict[str, object]]:
        self.counter += 1
        code, payload, stdout, stderr = self.run_capture(
            [
                "capture",
                "prepare",
                "--thread-id",
                "thread-a",
                "--turn-id",
                f"turn-{self.counter}",
                "--product",
                "anheng",
                "--template-id",
                template_id,
            ],
            template_root=template_root,
        )
        self.assertEqual(0, code, (payload, stdout, stderr))
        return payload["data"]["operation_id"], payload

    def inventory_running(self) -> str:
        operation_id, _ = self.prepare()
        code, _, _, _ = self.run_capture(
            [
                "capture",
                "attach",
                "--operation-id",
                operation_id,
                "--fork-thread-id",
                "thread-fork",
            ]
        )
        self.assertEqual(0, code)
        code, _, _, _ = self.run_capture(
            [
                "capture",
                "attach-turn",
                "--operation-id",
                operation_id,
                "--stage",
                "inventory",
                "--turn-id",
                "turn-inventory",
            ]
        )
        self.assertEqual(0, code)
        return operation_id

    def inventory_completed(self, inventory: object = VALID_INVENTORY) -> str:
        operation_id = self.inventory_running()
        code, _, _, _ = self.run_capture(
            [
                "capture",
                "complete-inventory",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin=json.dumps(inventory, ensure_ascii=False),
        )
        self.assertEqual(0, code)
        return operation_id

    def extraction_running(self, inventory: object = VALID_INVENTORY) -> str:
        operation_id = self.inventory_completed(inventory)
        code, _, _, _ = self.run_capture(
            [
                "capture",
                "attach-turn",
                "--operation-id",
                operation_id,
                "--stage",
                "extraction",
                "--turn-id",
                "turn-extraction",
            ]
        )
        self.assertEqual(0, code)
        return operation_id

    def complete(
        self,
        extraction: object | None = None,
        inventory: object = VALID_INVENTORY,
    ) -> str:
        operation_id = self.extraction_running(inventory)
        value = {"candidates": []} if extraction is None else extraction
        code, _, _, _ = self.run_capture(
            [
                "capture",
                "complete-extraction",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin=json.dumps(value, ensure_ascii=False),
        )
        self.assertEqual(0, code)
        return operation_id

    def assert_one_envelope(
        self, result: tuple[int, dict[str, object], str, str]
    ) -> None:
        _, _, stdout, stderr = result
        self.assertEqual(1, len(stdout.strip().splitlines()))
        self.assertNotIn("Traceback", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_prepare_emits_each_exact_prompt_once_with_template_metadata(self) -> None:
        code, payload, stdout, stderr = self.run_capture(
            [
                "capture",
                "prepare",
                "--thread-id",
                "thread-a",
                "--turn-id",
                "turn-7",
                "--product",
                "安恒",
            ]
        )

        self.assertEqual(0, code)
        self.assertEqual("capture.prepared", payload["kind"])
        data = payload["data"]
        self.assertEqual("business", data["template"]["template_id"])
        self.assertEqual(1, data["template"]["revision"])
        self.assertEqual("业务决策压缩模板", data["template"]["title"])
        self.assertRegex(data["template"]["content_digest"], r"^[0-9a-f]{12}$")
        self.assertIn("ZDECISION_CAPTURE_ARTIFACT_V2:inventory", data["inventory_prompt"])
        self.assertIn("ZDECISION_CAPTURE_ARTIFACT_V2:extract", data["extraction_prompt"])
        self.assertEqual("prepared", data["status"])
        self.assertFalse(data["replayed"])
        self.assertNotIn("template", data["record"] if "record" in data else {})
        self.assertEqual(1, self._count_key(payload, "inventory_prompt"))
        self.assertEqual(1, self._count_key(payload, "extraction_prompt"))
        self.assertEqual(1, len(stdout.strip().splitlines()))
        self.assertEqual("", stderr)

    @classmethod
    def _count_key(cls, value: object, key: str) -> int:
        if isinstance(value, dict):
            return int(key in value) + sum(
                cls._count_key(item, key) for item in value.values()
            )
        if isinstance(value, list):
            return sum(cls._count_key(item, key) for item in value)
        return 0

    def test_explicit_template_id_is_passed_to_catalog(self) -> None:
        copied = self.template_root
        architecture = copied / "architecture"
        shutil.copytree(copied / "business", architecture)
        manifest_path = architecture / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest.update(
            {"template_id": "architecture", "title": "架构决策模板", "revision": 2}
        )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")

        _, payload = self.prepare(
            template_id="architecture", template_root=copied
        )

        self.assertEqual("architecture", payload["data"]["template"]["template_id"])
        self.assertEqual("架构决策模板", payload["data"]["template"]["title"])

    def test_resume_returns_frozen_prompts_without_reading_live_catalog(self) -> None:
        operation_id, prepared = self.prepare(template_root=self.template_root)
        missing_root = self.root / "missing-templates"

        code, resumed, _, _ = self.run_capture(
            ["capture", "resume", "--operation-id", operation_id],
            template_root=missing_root,
        )

        self.assertEqual(0, code)
        self.assertTrue(resumed["data"]["replayed"])
        self.assertEqual(
            prepared["data"]["inventory_prompt"],
            resumed["data"]["inventory_prompt"],
        )
        self.assertEqual(
            prepared["data"]["extraction_prompt"],
            resumed["data"]["extraction_prompt"],
        )

    def test_full_two_stage_sequence_and_safe_show(self) -> None:
        inventory = {
            **VALID_INVENTORY,
            "coverage": {
                "reviewed_retained_context": "earliest_to_latest",
                "known_gaps": ["缺少权限边界确认"],
            },
        }
        operation_id = self.complete(
            {"candidates": [valid_candidate()]}, inventory
        )

        result = self.run_capture(
            ["capture", "show", "--operation-id", operation_id]
        )

        code, payload, stdout, stderr = result
        self.assertEqual(0, code)
        self.assertEqual("capture.shown", payload["kind"])
        data = payload["data"]
        self.assertEqual("completed", data["record"]["status"])
        self.assertRegex(data["record"]["inventory_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(data["record"]["extraction_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(["缺少权限边界确认"], data["known_gaps"])
        self.assertEqual("business", data["template"]["template_id"])
        self.assertEqual(
            "Keep private state outside Git.",
            data["candidates"][0]["content"]["claim"],
        )
        self.assertNotIn("signals", data)
        self.assertNotIn("inventory_prompt", stdout)
        self.assertNotIn("extraction_prompt", stdout)
        self.assertEqual("", stderr)

    def test_attach_completion_and_failure_responses_never_contain_prompts(self) -> None:
        operation_id, _ = self.prepare()
        commands = (
            [
                "capture",
                "attach",
                "--operation-id",
                operation_id,
                "--fork-thread-id",
                "thread-fork",
            ],
            [
                "capture",
                "attach-turn",
                "--operation-id",
                operation_id,
                "--stage",
                "inventory",
                "--turn-id",
                "turn-inventory",
            ],
        )
        for command in commands:
            result = self.run_capture(command)
            self.assertEqual(0, result[0])
            self.assertNotIn("inventory_prompt", result[2])
            self.assertNotIn("extraction_prompt", result[2])

        result = self.run_capture(
            [
                "capture",
                "fail-stage",
                "--operation-id",
                operation_id,
                "--stage",
                "inventory",
                "--code",
                "model_timeout",
                "--output-sha256",
                "1" * 64,
            ]
        )
        self.assertEqual(0, result[0])
        self.assertNotIn("inventory_prompt", result[2])
        self.assertNotIn("extraction_prompt", result[2])

    def test_extraction_completion_returns_the_public_capture_record(self) -> None:
        operation_id = self.extraction_running()

        result = self.run_capture(
            [
                "capture",
                "complete-extraction",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin='{"candidates":[]}',
        )

        self.assertEqual(0, result[0])
        data = result[1]["data"]
        self.assertEqual(2, data["record_version"])
        self.assertEqual("completed", data["status"])
        self.assertRegex(data["inventory_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(data["extraction_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual([], data["candidate_ids"])
        self.assertNotIn("inventory_prompt", result[2])
        self.assertNotIn("extraction_prompt", result[2])

    def test_empty_native_ids_emit_one_error_without_mutating_state(self) -> None:
        operation_id, _ = self.prepare()

        empty_fork = self.run_capture(
            [
                "capture",
                "attach",
                "--operation-id",
                operation_id,
                "--fork-thread-id",
                "",
            ]
        )
        self.assertEqual(2, empty_fork[0])
        self.assertEqual("invalid_arguments", empty_fork[1]["error"]["code"])
        self.assert_one_envelope(empty_fork)
        resumed = self.run_capture(
            ["capture", "resume", "--operation-id", operation_id]
        )
        self.assertEqual("prepared", resumed[1]["data"]["status"])

        attached = self.run_capture(
            [
                "capture",
                "attach",
                "--operation-id",
                operation_id,
                "--fork-thread-id",
                "thread-fork",
            ]
        )
        self.assertEqual(0, attached[0])
        empty_turn = self.run_capture(
            [
                "capture",
                "attach-turn",
                "--operation-id",
                operation_id,
                "--stage",
                "inventory",
                "--turn-id",
                "",
            ]
        )
        self.assertEqual(2, empty_turn[0])
        self.assertEqual("invalid_arguments", empty_turn[1]["error"]["code"])
        self.assert_one_envelope(empty_turn)
        resumed = self.run_capture(
            ["capture", "resume", "--operation-id", operation_id]
        )
        self.assertEqual("fork_attached", resumed[1]["data"]["status"])

    def test_invalid_json_hashes_raw_input_and_records_terminal_failure(self) -> None:
        operation_id = self.inventory_running()
        secret = "RAW_MODEL_SECRET_48f1"

        result = self.run_capture(
            [
                "capture",
                "complete-inventory",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin="{" + secret,
        )

        code, payload, stdout, stderr = result
        self.assertEqual(2, code)
        self.assertEqual("invalid_json", payload["error"]["code"])
        show = self.run_capture(["capture", "show", "--operation-id", operation_id])
        failure = show[1]["data"]["record"]["failure"]
        self.assertEqual("failed", show[1]["data"]["record"]["status"])
        self.assertEqual("invalid_json", failure["code"])
        self.assertRegex(failure["output_sha256"], r"^[0-9a-f]{64}$")
        private_text = next((self.state_dir / "captures").iterdir()).read_text("utf-8")
        self.assertNotIn(secret, private_text)
        self.assertNotIn(secret, stdout)
        self.assertNotIn(secret, stderr)
        self.assert_one_envelope(result)

    def test_invalid_utf8_input_file_records_only_its_digest(self) -> None:
        operation_id = self.inventory_running()
        raw = b"\xff\xfePRIVATE_MODEL_BYTES"
        input_path = self.root / "invalid-utf8.json"
        input_path.write_bytes(raw)

        result = self.run_capture(
            [
                "capture",
                "complete-inventory",
                "--operation-id",
                operation_id,
                "--input",
                str(input_path),
            ]
        )

        self.assertEqual(2, result[0])
        self.assertEqual("invalid_json", result[1]["error"]["code"])
        shown = self.run_capture(
            ["capture", "show", "--operation-id", operation_id]
        )
        failure = shown[1]["data"]["record"]["failure"]
        self.assertEqual(hashlib.sha256(raw).hexdigest(), failure["output_sha256"])
        self.assert_one_envelope(result)

    def test_stage_two_invalid_json_preserves_valid_inventory_only(self) -> None:
        operation_id = self.extraction_running()
        secret = "RAW_EXTRACTION_SECRET_771a"

        result = self.run_capture(
            [
                "capture",
                "complete-extraction",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin="{" + secret,
        )

        self.assertEqual(2, result[0])
        self.assertEqual("invalid_json", result[1]["error"]["code"])
        shown = self.run_capture(
            ["capture", "show", "--operation-id", operation_id]
        )
        self.assertEqual("failed", shown[1]["data"]["record"]["status"])
        self.assertEqual(
            "extraction", shown[1]["data"]["record"]["failure"]["stage"]
        )
        self.assertTrue(
            (self.state_dir / "inventories" / f"{operation_id}.json").exists()
        )
        self.assertFalse((self.state_dir / "candidates").exists())
        self.assertNotIn(secret, result[2] + result[3])

    def test_decoded_non_object_roots_use_stage_validation_codes(self) -> None:
        inventory_operation = self.inventory_running()
        result = self.run_capture(
            [
                "capture",
                "complete-inventory",
                "--operation-id",
                inventory_operation,
                "--input",
                "-",
            ],
            stdin="[]",
        )
        self.assertEqual(2, result[0])
        self.assertEqual("invalid_inventory", result[1]["error"]["code"])

        extraction_operation = self.extraction_running()
        result = self.run_capture(
            [
                "capture",
                "complete-extraction",
                "--operation-id",
                extraction_operation,
                "--input",
                "-",
            ],
            stdin="[]",
        )
        self.assertEqual(2, result[0])
        self.assertEqual("invalid_extraction", result[1]["error"]["code"])

    def test_inventory_count_and_size_failures_keep_their_stable_codes(self) -> None:
        cases = (
            (
                {"signals": [{}] * 101, "coverage": {}},
                "inventory_signal_limit_exceeded",
            ),
            (
                {
                    "signals": [{"private": "界" * 100_000}],
                    "coverage": {},
                },
                "inventory_output_too_large",
            ),
        )
        for value, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                operation_id = self.inventory_running()
                result = self.run_capture(
                    [
                        "capture",
                        "complete-inventory",
                        "--operation-id",
                        operation_id,
                        "--input",
                        "-",
                    ],
                    stdin=json.dumps(value, ensure_ascii=False),
                )
                self.assertEqual(2, result[0])
                self.assertEqual(expected_code, result[1]["error"]["code"])
                self.assertFalse((self.state_dir / "inventories" / f"{operation_id}.json").exists())

    def test_candidate_limit_failure_uses_stable_code_and_writes_none(self) -> None:
        operation_id = self.extraction_running()

        result = self.run_capture(
            [
                "capture",
                "complete-extraction",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin=json.dumps({"candidates": [{}] * 21}),
        )

        self.assertEqual(2, result[0])
        self.assertEqual("candidate_limit_exceeded", result[1]["error"]["code"])
        self.assertFalse((self.state_dir / "candidates").exists())

    def test_invalid_candidate_payload_never_echoes_model_authored_secret(self) -> None:
        operation_id = self.extraction_running()
        secret = "CANDIDATE_SECRET_11cd"
        candidate = {**valid_candidate(), secret: "private value"}

        result = self.run_capture(
            [
                "capture",
                "complete-extraction",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin=json.dumps({"candidates": [candidate]}),
        )

        self.assertEqual(2, result[0])
        self.assertEqual("invalid_extraction", result[1]["error"]["code"])
        self.assertNotIn(secret, result[2] + result[3])
        private_capture = (
            self.state_dir / "captures" / f"{operation_id}.json"
        ).read_text("utf-8")
        self.assertNotIn(secret, private_capture)

    def test_invalid_stage_order_uses_capture_action_required(self) -> None:
        operation_id, _ = self.prepare()

        result = self.run_capture(
            [
                "capture",
                "attach-turn",
                "--operation-id",
                operation_id,
                "--stage",
                "extraction",
                "--turn-id",
                "turn-extraction",
            ]
        )

        self.assertEqual(4, result[0])
        self.assertEqual("capture_action_required", result[1]["error"]["code"])

    def test_conflicting_fork_and_turn_attachment_use_exit_five(self) -> None:
        operation_id = self.inventory_running()
        for command, error_code in (
            (
                [
                    "capture",
                    "attach",
                    "--operation-id",
                    operation_id,
                    "--fork-thread-id",
                    "different-fork",
                ],
                "capture_fork_conflict",
            ),
            (
                [
                    "capture",
                    "attach-turn",
                    "--operation-id",
                    operation_id,
                    "--stage",
                    "inventory",
                    "--turn-id",
                    "different-turn",
                ],
                "capture_turn_conflict",
            ),
        ):
            with self.subTest(error_code=error_code):
                result = self.run_capture(command)
                self.assertEqual(5, result[0])
                self.assertEqual(error_code, result[1]["error"]["code"])

    def test_fail_stage_records_supported_terminal_failures(self) -> None:
        inventory_operation = self.inventory_running()
        result = self.run_capture(
            [
                "capture",
                "fail-stage",
                "--operation-id",
                inventory_operation,
                "--stage",
                "inventory",
                "--code",
                "model_timeout",
            ]
        )
        self.assertEqual(0, result[0])
        self.assertEqual("model_timeout", result[1]["data"]["failure"]["code"])

        extraction_operation = self.extraction_running()
        result = self.run_capture(
            [
                "capture",
                "fail-stage",
                "--operation-id",
                extraction_operation,
                "--stage",
                "extraction",
                "--code",
                "model_contract_violation",
                "--output-sha256",
                "2" * 64,
            ]
        )
        self.assertEqual(0, result[0])
        self.assertEqual(
            "model_contract_violation", result[1]["data"]["failure"]["code"]
        )

    def test_output_digest_argument_is_strict_and_never_mutates_state(self) -> None:
        operation_id = self.inventory_running()
        invalid_values = ("A" * 64, "a" * 63, "a" * 65, "g" * 64)

        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                result = self.run_capture(
                    [
                        "capture",
                        "fail-stage",
                        "--operation-id",
                        operation_id,
                        "--stage",
                        "inventory",
                        "--code",
                        "model_timeout",
                        "--output-sha256",
                        invalid,
                    ]
                )
                self.assertEqual(2, result[0])
                self.assertEqual("invalid_arguments", result[1]["error"]["code"])

        show = self.run_capture(["capture", "show", "--operation-id", operation_id])
        self.assertEqual("inventory_running", show[1]["data"]["record"]["status"])

    def test_unknown_or_invalid_template_stops_before_capture_state(self) -> None:
        for template_id in ("unknown", "../business"):
            with self.subTest(template_id=template_id):
                result = self.run_capture(
                    [
                        "capture",
                        "prepare",
                        "--thread-id",
                        "thread-a",
                        "--turn-id",
                        "turn-invalid",
                        "--product",
                        "anheng",
                        "--template-id",
                        template_id,
                    ]
                )
                self.assertEqual(2, result[0])
                self.assertEqual("invalid_template", result[1]["error"]["code"])

        self.assertFalse((self.state_dir / "captures").exists())

    def test_missing_or_digest_mismatched_inventory_stops_show_and_completion(self) -> None:
        from zdecision.jsonio import atomic_write_json

        for corruption in ("missing", "digest_mismatch"):
            with self.subTest(corruption=corruption):
                operation_id = self.extraction_running()
                path = self.state_dir / "inventories" / f"{operation_id}.json"
                if corruption == "missing":
                    path.unlink()
                else:
                    atomic_write_json(
                        path,
                        {
                            "signals": [],
                            "coverage": {
                                "reviewed_retained_context": "earliest_to_latest",
                                "known_gaps": ["changed"],
                            },
                        },
                    )

                for command in (
                    ["capture", "show", "--operation-id", operation_id],
                    [
                        "capture",
                        "complete-extraction",
                        "--operation-id",
                        operation_id,
                        "--input",
                        "-",
                    ],
                ):
                    result = self.run_capture(
                        command,
                        stdin='{"candidates":[]}',
                    )
                    self.assertEqual(4, result[0])
                    self.assertEqual(
                        "capture_action_required", result[1]["error"]["code"]
                    )
                    self.assertNotIn("known_gaps", result[2])

    def test_ambiguous_prepare_retry_and_missing_capture_keep_stable_codes(self) -> None:
        operation_id, prepared = self.prepare()
        source = prepared["data"]["source"]

        ambiguous = self.run_capture(
            [
                "capture",
                "prepare",
                "--thread-id",
                source["thread_id"],
                "--turn-id",
                source["turn_id"],
                "--product",
                "anheng",
            ]
        )
        self.assertEqual(5, ambiguous[0])
        self.assertEqual("capture_fork_ambiguous", ambiguous[1]["error"]["code"])
        self.assertEqual(
            operation_id, ambiguous[1]["error"]["details"]["operation_id"]
        )

        missing = self.run_capture(
            ["capture", "show", "--operation-id", "cap_" + "f" * 32]
        )
        self.assertEqual(3, missing[0])
        self.assertEqual("capture_not_found", missing[1]["error"]["code"])

    def test_corrupt_private_capture_inventory_and_candidate_are_sanitized(self) -> None:
        secret = "PRIVATE_FILE_SECRET_90ac"

        capture_operation, _ = self.prepare()
        capture_path = self.state_dir / "captures" / f"{capture_operation}.json"
        capture_path.write_text("{" + secret, "utf-8")
        self._assert_private_state_invalid(capture_operation, secret)

        inventory_operation = self.inventory_completed()
        inventory_path = (
            self.state_dir / "inventories" / f"{inventory_operation}.json"
        )
        inventory_path.write_text(json.dumps({secret: "typed invalid"}), "utf-8")
        self._assert_private_state_invalid(inventory_operation, secret)

        candidate_operation = self.complete({"candidates": [valid_candidate()]})
        shown = self.run_capture(["capture", "show", "--operation-id", candidate_operation])
        candidate_id = shown[1]["data"]["record"]["candidate_ids"][0]
        candidate_path = self.state_dir / "candidates" / f"{candidate_id}.json"
        candidate_path.write_text("{" + secret, "utf-8")
        self._assert_private_state_invalid(candidate_operation, secret)

    def _assert_private_state_invalid(self, operation_id: str, secret: str) -> None:
        result = self.run_capture(["capture", "show", "--operation-id", operation_id])
        self.assertEqual(3, result[0])
        self.assertEqual("private_state_invalid", result[1]["error"]["code"])
        self.assertNotIn(secret, result[2])
        self.assertNotIn(secret, result[3])
        self.assert_one_envelope(result)

    def test_legacy_completed_record_and_candidates_are_display_only(self) -> None:
        from zdecision.capture.models import Candidate, CandidateContent, SourceCheckpoint
        from zdecision.jsonio import atomic_write_json
        from zdecision.private_store.filesystem import FilePrivateStore

        operation_id = "cap_" + "a" * 32
        candidate_id = "cand_old_01"
        atomic_write_json(
            self.state_dir / "captures" / f"{operation_id}.json",
            {
                "operation_id": operation_id,
                "source": {"thread_id": "thread-old", "turn_id": "turn-old"},
                "product": "anheng",
                "status": "completed",
                "fork_thread_id": "thread-old-fork",
                "candidate_ids": [candidate_id],
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-01T00:01:00Z",
            },
        )
        FilePrivateStore(self.state_dir).put_candidate(
            Candidate(
                candidate_id=candidate_id,
                capture_id=operation_id,
                ordinal=1,
                content=CandidateContent(
                    product="anheng",
                    claim="Legacy candidate",
                    future_action="Display only",
                    scope_summary="legacy",
                    repositories=(),
                    paths=(),
                    invalidation_conditions=(),
                ),
                source=SourceCheckpoint("thread-old", "turn-old"),
            )
        )

        shown = self.run_capture(["capture", "show", "--operation-id", operation_id])

        self.assertEqual(0, shown[0])
        self.assertTrue(shown[1]["data"]["legacy"])
        self.assertEqual(
            "Legacy candidate", shown[1]["data"]["candidates"][0]["content"]["claim"]
        )
        mutation = self.run_capture(
            [
                "capture",
                "attach",
                "--operation-id",
                operation_id,
                "--fork-thread-id",
                "new-fork",
            ]
        )
        self.assertEqual(4, mutation[0])

    def test_v1_complete_action_is_no_longer_callable(self) -> None:
        result = self.run_capture(
            [
                "capture",
                "complete",
                "--operation-id",
                "cap_" + "a" * 32,
                "--input",
                "-",
            ],
            stdin='{"candidates":[]}',
        )

        self.assertEqual(2, result[0])
        self.assertEqual("invalid_arguments", result[1]["error"]["code"])

    def test_python_module_entry_point_uses_same_cli(self) -> None:
        environment = os.environ.copy()
        environment["ZDECISION_STATE_DIR"] = str(self.state_dir)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "zdecision",
                "capture",
                "prepare",
                "--thread-id",
                "thread-module",
                "--turn-id",
                "turn-1",
                "--product",
                "anheng",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("capture.prepared", json.loads(result.stdout)["kind"])
        self.assertEqual(1, len(result.stdout.strip().splitlines()))


if __name__ == "__main__":
    unittest.main()
