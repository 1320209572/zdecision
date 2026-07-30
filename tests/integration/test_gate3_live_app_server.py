from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest


SESSION_ID = os.environ.get("ZDECISION_GATE3_SESSION_ID")
TURN_ID = os.environ.get("ZDECISION_GATE3_TURN_ID")
LIVE_ACCEPTANCE = bool(SESSION_ID and TURN_ID)


@unittest.skipUnless(
    LIVE_ACCEPTANCE,
    "set the real Gate 3 Session and completed Turn IDs for explicit acceptance",
)
class Gate3LiveAppServerTests(unittest.TestCase):
    def _run_gate3(self) -> dict[str, object]:
        executable = shutil.which("zdecision-agent")
        self.assertIsNotNone(executable)
        completed = subprocess.run(
            [
                executable,
                "gate3",
                "--session-id",
                SESSION_ID,
                "--turn-id",
                TURN_ID,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertIsInstance(value, dict)
        return value

    def test_real_hook_boundary_runs_once_and_replays_without_new_capture(self):
        first = self._run_gate3()

        self.assertEqual("completed", first["state"])
        self.assertEqual(SESSION_ID, first["source_thread_id"])
        self.assertEqual(TURN_ID, first["source_turn_id"])
        self.assertIsNotNone(first["capture_operation_id"])
        self.assertIsNotNone(first["capture_thread_id"])
        self.assertNotEqual(
            first["assessment_thread_id"], first["capture_thread_id"]
        )
        self.assertIsNotNone(first["inventory_turn_id"])
        self.assertIsNotNone(first["extraction_turn_id"])
        self.assertGreaterEqual(first["candidate_count"], 0)
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("candidates", serialized)
        self.assertNotIn("source_content", serialized)
        self.assertNotIn("transcript", serialized)

        replay = self._run_gate3()

        for field_name in (
            "automated_capture_id",
            "assessment_thread_id",
            "assessment_turn_id",
            "capture_operation_id",
            "capture_thread_id",
            "inventory_turn_id",
            "extraction_turn_id",
            "candidate_count",
            "eligibility_input_digest",
            "eligibility_prompt_digest",
            "model",
            "state",
        ):
            self.assertEqual(first[field_name], replay[field_name])
        self.assertEqual("replay", replay["route"])


if __name__ == "__main__":
    unittest.main()
