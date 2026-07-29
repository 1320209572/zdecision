from __future__ import annotations

import io
import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import termios
import unittest
from pathlib import Path

from tests.test_inventory import VALID_INVENTORY
from zdecision.capture.service import CaptureService
from zdecision.capture.templates import TemplateCatalog
from zdecision.jsonio import atomic_write_json, canonical_json_bytes
from zdecision.private_store.filesystem import FilePrivateStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = (
    REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"
)
PRODUCT = "安恒"


class ReviewPublishCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.origin = self.root / "origin.git"
        self.repository = self.root / "repository"
        self.template_root = self.root / "templates"
        shutil.copytree(TEMPLATE_ROOT, self.template_root)

        self.git(
            self.root,
            "git",
            "init",
            "--bare",
            "--initial-branch=main",
            str(self.origin),
        )
        self.git(
            self.root,
            "git",
            "init",
            "--initial-branch=main",
            str(self.repository),
        )
        self.git(self.repository, "git", "config", "user.name", "CLI Test")
        self.git(
            self.repository,
            "git",
            "config",
            "user.email",
            "cli@example.invalid",
        )
        registry = self.repository / "decision-registry"
        registry.mkdir()
        atomic_write_json(
            registry / "registry.json",
            {
                "format": "zdecision-registry/v1",
                "schema_version": 1,
                "products": {},
            },
        )
        (registry / "README.md").write_text("formal only\n", "utf-8")
        self.git(self.repository, "git", "add", ".")
        self.git(self.repository, "git", "commit", "-m", "initial registry")
        self.git(self.repository, "git", "remote", "add", "origin", str(self.origin))
        self.git(self.repository, "git", "push", "-u", "origin", "main")

        self.environ = {
            "ZDECISION_STATE_DIR": str(self.state_dir),
            "ZDECISION_REPOSITORY_ROOT": str(self.repository),
            "ZDECISION_EXPECTED_ORIGIN": str(self.origin),
            "ZDECISION_TEMPLATE_ROOT": str(self.template_root),
        }
        self.store = FilePrivateStore(self.state_dir)
        self.capture_service = CaptureService(
            self.store,
            TemplateCatalog(self.template_root, ENVELOPE_ROOT),
        )
        self.operation_id = self.complete_capture()
        capture = self.store.get_capture(self.operation_id)
        self.candidate_ids = capture.candidate_ids

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def complete_capture(
        self,
        *,
        candidate_count: int = 4,
        source_turn_id: str = "turn-source",
    ) -> str:
        plan = self.capture_service.prepare(
            "thread-source",
            source_turn_id,
            PRODUCT,
            "business",
        )
        operation_id = plan.record.operation_id
        self.capture_service.attach_fork(operation_id, "thread-fork")
        self.capture_service.attach_stage_turn(
            operation_id, "inventory", "turn-inventory"
        )
        self.capture_service.complete_inventory(operation_id, VALID_INVENTORY)
        self.capture_service.attach_stage_turn(
            operation_id, "extraction", "turn-extraction"
        )
        candidates = []
        for ordinal in range(1, candidate_count + 1):
            candidates.append(
                {
                    "product": PRODUCT,
                    "claim": f"正式业务规则 {ordinal}。",
                    "future_action": f"执行正式业务规则 {ordinal}。",
                    "scope": {
                        "summary": "ZDecision Review Publish",
                        "repositories": [
                            "https://github.com/1320209572/zdecision.git"
                        ],
                        "paths": ["decision-registry/"],
                    },
                    "invalidation_conditions": ["新的正式规则替代当前规则"],
                }
            )
        self.capture_service.complete_extraction(
            operation_id,
            {"candidates": candidates},
        )
        return operation_id

    def run_cli(
        self,
        argv: list[str],
        *,
        stdin: str = "",
    ) -> tuple[int, dict[str, object], str, str]:
        from zdecision.cli import main

        stdout_stream = io.StringIO()
        stderr_stream = io.StringIO()
        code = main(
            argv,
            io.StringIO(stdin),
            stdout_stream,
            stderr_stream,
            self.environ,
        )
        stdout = stdout_stream.getvalue()
        return code, json.loads(stdout), stdout, stderr_stream.getvalue()

    def review_input(self, *, accepted: bool = True) -> dict[str, object]:
        if not accepted:
            return {
                "items": [
                    {"candidate_id": self.candidate_ids[0], "action": "reject"},
                    {"candidate_id": self.candidate_ids[1], "action": "skip"},
                ]
            }
        second = self.store.get_candidate(self.candidate_ids[1])
        content = second.content.to_dict()
        content["claim"] = "发布前展示完整正式内容与路径。"
        return {
            "items": [
                {"candidate_id": self.candidate_ids[0], "action": "accept"},
                {
                    "candidate_id": self.candidate_ids[1],
                    "action": "edit_accept",
                    "content": content,
                },
                {"candidate_id": self.candidate_ids[2], "action": "reject"},
                {"candidate_id": self.candidate_ids[3], "action": "skip"},
            ]
        }

    def record_review(self, *, accepted: bool = True) -> str:
        result = self.run_cli(
            [
                "review",
                "record",
                "--operation-id",
                self.operation_id,
                "--approval-thread-id",
                "thread-review",
                "--approval-turn-id",
                "turn-review",
                "--input",
                "-",
            ],
            stdin=json.dumps(self.review_input(accepted=accepted), ensure_ascii=False),
        )
        self.assertEqual(0, result[0], result)
        return result[1]["data"]["review_batch_id"]

    def assert_success_envelope(
        self,
        result: tuple[int, dict[str, object], str, str],
    ) -> None:
        code, payload, stdout, stderr = result
        self.assertEqual(0, code, result)
        self.assertEqual("", stderr)
        self.assertEqual(canonical_json_bytes(payload).decode("utf-8"), stdout)
        self.assertEqual(1, len(stdout.strip().splitlines()))

    def test_all_six_operations_use_one_canonical_json_envelope(self) -> None:
        review = self.run_cli(
            [
                "review",
                "record",
                "--operation-id",
                self.operation_id,
                "--approval-thread-id",
                "thread-review",
                "--approval-turn-id",
                "turn-review",
                "--input",
                "-",
            ],
            stdin=json.dumps(self.review_input(), ensure_ascii=False),
        )
        self.assert_success_envelope(review)
        review_batch_id = review[1]["data"]["review_batch_id"]
        self.assertEqual("review.recorded", review[1]["kind"])
        self.assertEqual(4, len(review[1]["data"]["items"]))

        shown_review = self.run_cli(
            ["review", "show", "--review-batch-id", review_batch_id]
        )
        self.assert_success_envelope(shown_review)
        self.assertEqual(review[1]["data"], shown_review[1]["data"])

        before_preview = self.git(
            self.repository, "git", "status", "--porcelain=v1"
        ).stdout
        preview = self.run_cli(
            ["publish", "preview", "--review-batch-id", review_batch_id]
        )
        self.assert_success_envelope(preview)
        self.assertEqual("publication.previewed", preview[1]["kind"])
        preview_id = preview[1]["data"]["preview_id"]
        self.assertEqual("previewed", preview[1]["data"]["state"])
        self.assertEqual(2, len(preview[1]["data"]["decision_ids"]))
        self.assertEqual(
            before_preview,
            self.git(self.repository, "git", "status", "--porcelain=v1").stdout,
        )

        shown_preview = self.run_cli(
            ["publish", "show", "--preview-id", preview_id]
        )
        self.assert_success_envelope(shown_preview)
        self.assertEqual(preview[1]["data"], shown_preview[1]["data"])

        confirmed = self.run_cli(
            [
                "publish",
                "confirm",
                "--preview-id",
                preview_id,
                "--approval-thread-id",
                "thread-publish",
                "--approval-turn-id",
                "turn-publish",
            ]
        )
        self.assert_success_envelope(confirmed)
        self.assertEqual("publication.completed", confirmed[1]["kind"])
        self.assertEqual("completed", confirmed[1]["data"]["status"])

        resumed = self.run_cli(
            ["publish", "resume", "--preview-id", preview_id]
        )
        self.assert_success_envelope(resumed)
        self.assertEqual(confirmed[1]["data"], resumed[1]["data"])

    def test_review_stdin_is_strict_and_private_even_in_subprocess_errors(self) -> None:
        secret = "RAW_REVIEW_JSON_SECRET_91a7"
        invalid = {
            "items": [
                {
                    "candidate_id": self.candidate_ids[0],
                    "action": "accept",
                    "unexpected": secret,
                }
            ]
        }
        argv = [
            sys.executable,
            "-m",
            "zdecision",
            "review",
            "record",
            "--operation-id",
            self.operation_id,
            "--approval-thread-id",
            "thread-review",
            "--approval-turn-id",
            "turn-review",
            "--input",
            "-",
        ]
        child_environ = {**os.environ, **self.environ}
        self.assertNotIn(secret, "\0".join(argv))
        self.assertNotIn(secret, "\0".join(child_environ.values()))

        result = subprocess.run(
            argv,
            cwd=REPOSITORY_ROOT,
            env=child_environ,
            input=json.dumps(invalid, ensure_ascii=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(2, result.returncode)
        self.assertEqual("invalid_review", payload["error"]["code"])
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertFalse((self.state_dir / "review_batches").exists())

    def test_noncanonical_no_echo_pty_accepts_review_larger_than_max_canon(self) -> None:
        operation_id = self.complete_capture(
            candidate_count=14,
            source_turn_id="turn-source-pty",
        )
        candidate_ids = self.store.get_capture(operation_id).candidate_ids
        raw = json.dumps(
            {
                "items": [
                    {"candidate_id": candidate_id, "action": "accept"}
                    for candidate_id in candidate_ids
                ]
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertGreater(len(raw), 1024)
        master_fd, slave_fd = pty.openpty()
        attributes = termios.tcgetattr(slave_fd)
        attributes[3] &= ~(termios.ECHO | termios.ICANON)
        attributes[6][termios.VMIN] = 1
        attributes[6][termios.VTIME] = 0
        termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
        child_environ = {**os.environ, **self.environ}
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "zdecision",
                "review",
                "record",
                "--operation-id",
                operation_id,
                "--approval-thread-id",
                "thread-review",
                "--approval-turn-id",
                "turn-review-pty",
                "--input",
                "-",
            ],
            cwd=REPOSITORY_ROOT,
            env=child_environ,
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(slave_fd)
        try:
            os.write(master_fd, raw + b"\x04")
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("CLI did not consume the noncanonical PTY EOT delimiter")
        finally:
            os.close(master_fd)

        payload = json.loads(stdout.decode("utf-8"))
        self.assertEqual(0, process.returncode, (stdout, stderr))
        self.assertEqual("review.recorded", payload["kind"])
        self.assertEqual(14, len(payload["data"]["items"]))
        self.assertEqual(b"", stderr)

    def test_review_parser_rejects_non_object_extra_root_and_non_stdin_input(self) -> None:
        cases = (
            ("[]", "-"),
            ('{"items":[],"extra":true}', "-"),
            ('{"items":[]}', "private-review.json"),
        )
        for raw, input_name in cases:
            with self.subTest(input_name=input_name, raw=raw):
                result = self.run_cli(
                    [
                        "review",
                        "record",
                        "--operation-id",
                        self.operation_id,
                        "--approval-thread-id",
                        "thread-review",
                        "--approval-turn-id",
                        "turn-review",
                        "--input",
                        input_name,
                    ],
                    stdin=raw,
                )
                self.assertEqual(2, result[0])
                expected = (
                    "invalid_arguments"
                    if input_name != "-"
                    else "invalid_review"
                )
                self.assertEqual(expected, result[1]["error"]["code"])

    def test_error_codes_and_exit_classes_are_stable(self) -> None:
        missing_review = self.run_cli(
            [
                "review",
                "show",
                "--review-batch-id",
                "rvb_" + "a" * 32,
            ]
        )
        self.assertEqual(3, missing_review[0])
        self.assertEqual("review_not_found", missing_review[1]["error"]["code"])

        missing_publication = self.run_cli(
            [
                "publish",
                "show",
                "--preview-id",
                "pub_" + "a" * 32,
            ]
        )
        self.assertEqual(3, missing_publication[0])
        self.assertEqual(
            "publication_not_found",
            missing_publication[1]["error"]["code"],
        )

        rejected_batch = self.record_review(accepted=False)
        no_items = self.run_cli(
            ["publish", "preview", "--review-batch-id", rejected_batch]
        )
        self.assertEqual(4, no_items[0])
        self.assertEqual(
            "no_publishable_items", no_items[1]["error"]["code"]
        )

    def test_cli_accepts_no_decision_payload_or_confirmation_phrase(self) -> None:
        review_batch_id = self.record_review()
        preview = self.run_cli(
            ["publish", "preview", "--review-batch-id", review_batch_id]
        )
        preview_id = preview[1]["data"]["preview_id"]

        for command in (
            [
                "publish",
                "preview",
                "--review-batch-id",
                review_batch_id,
                "--decision-json",
                "{}",
            ],
            [
                "publish",
                "confirm",
                "--preview-id",
                preview_id,
                "--approval-thread-id",
                "thread-publish",
                "--approval-turn-id",
                "turn-publish",
                "--confirmation",
                "确认发布",
            ],
        ):
            with self.subTest(command=command):
                result = self.run_cli(command)
                self.assertEqual(2, result[0])
                self.assertEqual("invalid_arguments", result[1]["error"]["code"])
        self.assertEqual(
            "previewed",
            self.run_cli(
                ["publish", "show", "--preview-id", preview_id]
            )[1]["data"]["state"],
        )


if __name__ == "__main__":
    unittest.main()
