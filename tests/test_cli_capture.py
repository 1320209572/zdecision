from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def run_cli(
    argv: list[str],
    *,
    state_dir: Path,
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
        {"ZDECISION_STATE_DIR": str(state_dir)},
    )
    stdout = stdout_stream.getvalue()
    return code, json.loads(stdout), stdout, stderr_stream.getvalue()


class CaptureCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_dir = Path(self.temp_dir.name)

    def prepare(self) -> str:
        code, payload, _, _ = run_cli(
            [
                "capture",
                "prepare",
                "--thread-id",
                "thread-a",
                "--turn-id",
                "turn-7",
                "--product",
                "anheng",
            ],
            state_dir=self.state_dir,
        )
        self.assertEqual(0, code)
        return payload["data"]["operation_id"]

    def prepare_and_attach(self) -> str:
        operation_id = self.prepare()
        code, _, _, _ = run_cli(
            [
                "capture",
                "attach",
                "--operation-id",
                operation_id,
                "--fork-thread-id",
                "thread-fork",
            ],
            state_dir=self.state_dir,
        )
        self.assertEqual(0, code)
        return operation_id

    def test_capture_prepare_emits_one_machine_envelope(self) -> None:
        code, payload, stdout, stderr = run_cli(
            [
                "capture",
                "prepare",
                "--thread-id",
                "thread-a",
                "--turn-id",
                "turn-7",
                "--product",
                "anheng",
            ],
            state_dir=self.state_dir,
        )

        self.assertEqual(0, code)
        self.assertEqual("capture.prepared", payload["kind"])
        self.assertTrue(payload["data"]["extraction_prompt"])
        self.assertRegex(payload["data"]["operation_id"], r"^cap_[0-9a-f]{32}$")
        self.assertEqual(1, len(stdout.strip().splitlines()))
        self.assertEqual("", stderr)

    def test_capture_attach_emits_attached_record(self) -> None:
        operation_id = self.prepare()

        code, payload, _, _ = run_cli(
            [
                "capture",
                "attach",
                "--operation-id",
                operation_id,
                "--fork-thread-id",
                "thread-fork",
            ],
            state_dir=self.state_dir,
        )

        self.assertEqual(0, code)
        self.assertEqual("capture.fork_attached", payload["kind"])
        self.assertEqual("thread-fork", payload["data"]["fork_thread_id"])

    def test_capture_complete_reads_json_from_stdin(self) -> None:
        operation_id = self.prepare_and_attach()

        code, payload, _, _ = run_cli(
            [
                "capture",
                "complete",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin=json.dumps({"candidates": []}),
            state_dir=self.state_dir,
        )

        self.assertEqual(0, code)
        self.assertEqual("capture.completed", payload["kind"])
        self.assertEqual([], payload["data"]["candidate_ids"])

    def test_capture_show_returns_private_candidate_fields_for_review(self) -> None:
        operation_id = self.prepare_and_attach()
        extraction = {
            "candidates": [
                {
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
            ]
        }
        code, _, _, _ = run_cli(
            [
                "capture",
                "complete",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin=json.dumps(extraction),
            state_dir=self.state_dir,
        )
        self.assertEqual(0, code)

        code, payload, _, _ = run_cli(
            ["capture", "show", "--operation-id", operation_id],
            state_dir=self.state_dir,
        )

        self.assertEqual(0, code)
        self.assertEqual("capture.shown", payload["kind"])
        self.assertEqual("completed", payload["data"]["record"]["status"])
        self.assertEqual(
            "Keep private state outside Git.",
            payload["data"]["candidates"][0]["content"]["claim"],
        )

    def test_invalid_json_uses_exit_2_and_no_traceback_on_stdout(self) -> None:
        code, payload, stdout, stderr = run_cli(
            [
                "capture",
                "complete",
                "--operation-id",
                "cap_bad",
                "--input",
                "-",
            ],
            stdin="{",
            state_dir=self.state_dir,
        )

        self.assertEqual(2, code)
        self.assertEqual("invalid_json", payload["error"]["code"])
        self.assertNotIn("Traceback", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_invalid_extraction_uses_exit_2(self) -> None:
        operation_id = self.prepare_and_attach()

        code, payload, _, _ = run_cli(
            [
                "capture",
                "complete",
                "--operation-id",
                operation_id,
                "--input",
                "-",
            ],
            stdin=json.dumps({"candidates": [], "summary": "forbidden"}),
            state_dir=self.state_dir,
        )

        self.assertEqual(2, code)
        self.assertEqual("invalid_extraction", payload["error"]["code"])

    def test_unattached_prepare_retry_uses_exit_5(self) -> None:
        operation_id = self.prepare()

        code, payload, _, _ = run_cli(
            [
                "capture",
                "prepare",
                "--thread-id",
                "thread-a",
                "--turn-id",
                "turn-7",
                "--product",
                "anheng",
            ],
            state_dir=self.state_dir,
        )

        self.assertEqual(5, code)
        self.assertEqual("capture_fork_ambiguous", payload["error"]["code"])
        self.assertEqual(
            operation_id,
            payload["error"]["details"]["operation_id"],
        )

    def test_missing_capture_uses_exit_3(self) -> None:
        code, payload, _, _ = run_cli(
            ["capture", "show", "--operation-id", "cap_" + "f" * 32],
            state_dir=self.state_dir,
        )

        self.assertEqual(3, code)
        self.assertEqual("capture_not_found", payload["error"]["code"])

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


if __name__ == "__main__":
    unittest.main()
