from __future__ import annotations

import contextlib
import hashlib
import io
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from zdecision.agent.repository import RepositoryResolver

try:
    from zdecision.central.cli import main
except ModuleNotFoundError as error:
    CONFIG_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    CONFIG_IMPORT_ERROR = None


class DemoConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            CONFIG_IMPORT_ERROR,
            f"Central CLI is missing: {CONFIG_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "ZDecision Tests")
        (self.repository / "README.md").write_text("fixture\n", "utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "fixture")
        self._git(
            "remote", "add", "origin", "https://github.com/OpenAI/example.git"
        )

    def tearDown(self) -> None:
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_init_separates_device_secret_and_writes_owner_only_files(self) -> None:
        output_directory = self.root / "demo-config"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(
                [
                    "demo-config",
                    "init",
                    "--repository-cwd",
                    str(self.repository),
                    "--product-name",
                    "Cloud",
                    "--output-dir",
                    str(output_directory),
                ]
            )

        self.assertEqual(0, result)
        central_path = output_directory / "central.json"
        agent_path = output_directory / "agent.json"
        self.assertEqual(0o600, stat.S_IMODE(central_path.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(agent_path.stat().st_mode))
        central = json.loads(central_path.read_text("utf-8"))
        agent = json.loads(agent_path.read_text("utf-8"))
        snapshot = RepositoryResolver(timeout_seconds=1.0).resolve(self.repository)
        self.assertIsNotNone(snapshot)
        expected_repository = {
            "repository_id": snapshot.repository_id,
            "enabled": True,
        }
        self.assertEqual([expected_repository], central["repositories"])
        self.assertEqual([expected_repository], agent["repositories"])
        self.assertEqual("Cloud", central["decision_spaces"][0]["display_name"])
        self.assertEqual("Shared", central["catalog_groups"][0]["display_name"])
        self.assertTrue(central["repository_routes"])
        self.assertEqual(central["organization_id"], agent["organization_id"])
        self.assertEqual(central["device_id"], agent["device_id"])
        self.assertNotIn("device_token", central)
        self.assertNotIn("device_token_sha256", agent)
        raw_token = agent["device_token"]
        digest = central["device_token_sha256"]
        self.assertEqual(hashlib.sha256(raw_token.encode()).hexdigest(), digest)
        captured = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(raw_token, captured)
        self.assertNotIn(digest, captured)

    def test_init_refuses_a_nonempty_output_directory(self) -> None:
        output_directory = self.root / "existing"
        output_directory.mkdir()
        marker = output_directory / "keep.txt"
        marker.write_text("keep\n", "utf-8")

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            result = main(
                [
                    "demo-config",
                    "init",
                    "--repository-cwd",
                    str(self.repository),
                    "--product-name",
                    "ZDecision",
                    "--output-dir",
                    str(output_directory),
                ]
            )

        self.assertEqual(1, result)
        self.assertEqual("keep\n", marker.read_text("utf-8"))
        self.assertFalse((output_directory / "central.json").exists())
        self.assertFalse((output_directory / "agent.json").exists())

    def test_run_refuses_a_non_loopback_bind_before_opening_files(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(
                [
                    "run",
                    "--database",
                    str(self.root / "missing.sqlite3"),
                    "--config",
                    str(self.root / "missing.json"),
                    "--registry-repository-root",
                    str(self.repository),
                    "--host",
                    "0.0.0.0",
                ]
            )

        self.assertEqual(1, result)
        self.assertEqual(
            {"error": "non_loopback_bind_forbidden"},
            json.loads(stderr.getvalue()),
        )
        self.assertEqual("", stdout.getvalue())
        self.assertFalse((self.root / "missing.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
