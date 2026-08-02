from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from zdecision.agent.launchd import (
        LABEL,
        install_launch_agent,
        render_launch_agent,
        uninstall_launch_agent,
    )
except ModuleNotFoundError as error:
    LAUNCHD_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    LAUNCHD_IMPORT_ERROR = None


DEVICE_TOKEN = "device-secret-token"


def compact(value: str) -> str:
    return "".join(line.strip() for line in value.splitlines())


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: list[str]) -> None:
        self.commands.append(tuple(command))


class LaunchAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            LAUNCHD_IMPORT_ERROR,
            f"LaunchAgent support is missing: {LAUNCHD_IMPORT_ERROR}",
        )

    def test_plist_runs_persistent_service_without_secrets_in_arguments(self) -> None:
        launch_path = "/opt/codex/bin:/opt/node/bin:/usr/bin:/bin"
        with patch.dict("os.environ", {"PATH": launch_path}):
            rendered = render_launch_agent(
                executable="/opt/zdecision/bin/zdecision-agent",
                state_dir="/Users/demo/Library/Application Support/ZDecision",
                config_path=(
                    "/Users/demo/Library/Application Support/ZDecision/agent.json"
                ),
            )
        parsed = plistlib.loads(rendered.encode("utf-8"))

        self.assertEqual(LABEL, parsed["Label"])
        self.assertEqual(
            [
                "/opt/zdecision/bin/zdecision-agent",
                "service",
                "run",
                "--config",
                "/Users/demo/Library/Application Support/ZDecision/agent.json",
            ],
            parsed["ProgramArguments"],
        )
        self.assertTrue(parsed["RunAtLoad"])
        self.assertTrue(parsed["KeepAlive"])
        self.assertEqual(10, parsed["ThrottleInterval"])
        self.assertEqual(
            launch_path,
            parsed["EnvironmentVariables"]["PATH"],
        )
        self.assertIn("<key>KeepAlive</key><true/>", compact(rendered))
        self.assertNotIn(DEVICE_TOKEN, rendered)

    def test_install_and_uninstall_touch_only_the_owned_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runner = RecordingRunner()
            path = install_launch_agent(
                executable="/opt/zdecision/bin/zdecision-agent",
                state_dir=str(home / "state"),
                config_path=str(home / "state" / "agent.json"),
                home=home,
                uid=501,
                runner=runner,
            )

            self.assertEqual(
                home
                / "Library"
                / "LaunchAgents"
                / "com.zdecision.agent.plist",
                path,
            )
            self.assertTrue(path.is_file())
            self.assertEqual(
                ("launchctl", "bootstrap", "gui/501", str(path)),
                runner.commands[0],
            )

            uninstall_launch_agent(
                home=home,
                uid=501,
                runner=runner,
            )
            self.assertFalse(path.exists())
            self.assertEqual(
                ("launchctl", "bootout", "gui/501", str(path)),
                runner.commands[1],
            )

    def test_uninstall_refuses_a_foreign_plist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            path.parent.mkdir(parents=True)
            path.write_bytes(
                plistlib.dumps(
                    {
                        "Label": "com.example.foreign",
                        "ProgramArguments": ["/bin/false"],
                    }
                )
            )
            runner = RecordingRunner()

            with self.assertRaises(ValueError):
                uninstall_launch_agent(home=home, uid=501, runner=runner)

            self.assertTrue(path.exists())
            self.assertEqual([], runner.commands)


if __name__ == "__main__":
    unittest.main()
