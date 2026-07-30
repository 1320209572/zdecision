from __future__ import annotations

import os
import shutil
import subprocess
import unittest


LIVE_ACCEPTANCE = os.environ.get("ZDECISION_LIVE_ACCEPTANCE") == "1"


@unittest.skipUnless(
    LIVE_ACCEPTANCE,
    "set ZDECISION_LIVE_ACCEPTANCE=1 only during explicit Gate 1 acceptance",
)
class Gate1PluginSmokeTests(unittest.TestCase):
    def test_global_agent_entrypoint_is_visible_to_the_desktop_path(self) -> None:
        executable = shutil.which("zdecision-agent")
        self.assertIsNotNone(executable)

        result = subprocess.run(
            [executable, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("test-repository", result.stdout)


if __name__ == "__main__":
    unittest.main()
