from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zdecision.agent.service import AgentServiceConfigError

try:
    from zdecision.agent.cli import config_locator_path
    from zdecision.agent.config_locator import (
        load_agent_config_path,
        publish_agent_config_locator,
    )
except ImportError as error:
    CONFIG_LOCATOR_IMPORT_ERROR: ImportError | None = error
else:
    CONFIG_LOCATOR_IMPORT_ERROR = None


class AgentConfigLocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            CONFIG_LOCATOR_IMPORT_ERROR,
            f"Agent config locator is missing: {CONFIG_LOCATOR_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.config = self.root / "owner-only-agent.json"
        self.config.write_text(
            json.dumps(
                {
                    "central_url": "http://127.0.0.1:8765",
                    "organization_id": "org_demo",
                    "device_id": "device_demo",
                    "device_token": "RAW_DEVICE_TOKEN_NEVER_LOCATED",
                    "repositories": [
                        {
                            "repository_id": "repo_" + "2" * 32,
                            "enabled": True,
                        }
                    ],
                }
            ),
            "utf-8",
        )
        os.chmod(self.config, 0o600)
        self.locator = self.root / "state" / "agent" / "config-locator.json"

    def test_fixed_locator_path_is_under_private_agent_state(self) -> None:
        path = config_locator_path({"ZDECISION_STATE_DIR": str(self.root / "state")})
        self.assertEqual(
            self.root / "state" / "agent" / "config-locator.json", path
        )

    def test_mcp_receives_the_fixed_locator_without_reading_config_in_cli(
        self,
    ) -> None:
        from zdecision.agent.cli import main

        state_root = self.root / "mcp-state"
        cwd = str(self.root / "repository")
        with (
            patch.dict(
                os.environ,
                {"ZDECISION_STATE_DIR": str(state_root)},
                clear=True,
            ),
            patch("zdecision.agent.cli.os.getcwd", return_value=cwd),
            patch("zdecision.agent.cli.run_mcp") as run_mcp,
        ):
            self.assertEqual(0, main(["mcp"]))

        run_mcp.assert_called_once_with(
            database_path=state_root / "agent" / "zdecision.sqlite3",
            config_locator_path=state_root / "agent" / "config-locator.json",
            recall_demo_config_path=state_root / "agent" / "recall-demo.json",
            cwd=cwd,
        )

    def test_publish_is_canonical_owner_only_and_atomically_replaceable(self) -> None:
        published = publish_agent_config_locator(self.locator, self.config)
        first_inode = self.locator.stat().st_ino

        replacement = self.root / "replacement-agent.json"
        replacement.write_bytes(self.config.read_bytes())
        os.chmod(replacement, 0o600)
        published = publish_agent_config_locator(self.locator, replacement)

        self.assertEqual(replacement, published)
        self.assertEqual(
            b'{"agent_config_path":"' + str(replacement).encode() + b'"}\n',
            self.locator.read_bytes(),
        )
        self.assertEqual(0o600, stat.S_IMODE(self.locator.stat().st_mode))
        self.assertEqual(os.getuid(), self.locator.stat().st_uid)
        self.assertNotEqual(first_inode, self.locator.stat().st_ino)
        self.assertEqual([], list(self.locator.parent.glob(".*.tmp")))
        self.assertEqual(replacement, load_agent_config_path(self.locator))
        self.assertNotIn(b"RAW_DEVICE_TOKEN_NEVER_LOCATED", self.locator.read_bytes())

    def test_rejects_relative_malformed_readable_nonowner_and_nonfile_locator(self) -> None:
        with self.assertRaises(AgentServiceConfigError):
            publish_agent_config_locator(Path("relative-locator.json"), self.config)
        with self.assertRaises(AgentServiceConfigError):
            publish_agent_config_locator(self.locator, Path("relative-config.json"))

        bad_values = (
            b"not-json\n",
            b'{"agent_config_path":"/tmp/a","device_token":"secret"}\n',
            b'{"agent_config_path":"relative.json"}\n',
        )
        self.locator.parent.mkdir(parents=True, exist_ok=True)
        for value in bad_values:
            with self.subTest(value=value):
                self.locator.write_bytes(value)
                os.chmod(self.locator, 0o600)
                with self.assertRaises(AgentServiceConfigError):
                    load_agent_config_path(self.locator)

        self.locator.write_text(
            json.dumps({"agent_config_path": str(self.config)}), "utf-8"
        )
        os.chmod(self.locator, 0o644)
        with self.assertRaises(AgentServiceConfigError):
            load_agent_config_path(self.locator)

        os.chmod(self.locator, 0o600)
        with patch("zdecision.agent.config_locator.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(AgentServiceConfigError):
                load_agent_config_path(self.locator)

        self.locator.unlink()
        self.locator.mkdir()
        with self.assertRaises(AgentServiceConfigError):
            load_agent_config_path(self.locator)

    def test_load_revalidates_target_config_permissions_owner_and_file_type(self) -> None:
        publish_agent_config_locator(self.locator, self.config)

        os.chmod(self.config, 0o640)
        with self.assertRaises(AgentServiceConfigError):
            load_agent_config_path(self.locator)

        os.chmod(self.config, 0o600)
        with patch("zdecision.agent.config_locator.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(AgentServiceConfigError):
                load_agent_config_path(self.locator)

        self.config.unlink()
        self.config.mkdir()
        with self.assertRaises(AgentServiceConfigError):
            load_agent_config_path(self.locator)


if __name__ == "__main__":
    unittest.main()
