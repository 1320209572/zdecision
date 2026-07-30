from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

try:
    from zdecision.agent.repository import RepositoryResolver
except ModuleNotFoundError as error:
    AGENT_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    AGENT_IMPORT_ERROR = None


EXPECTED_REPOSITORY_ID = "repo_941e46ac8a2d45bed98c625ebefc4b42"


class RepositoryResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            AGENT_IMPORT_ERROR,
            f"zdecision.agent runtime is missing: {AGENT_IMPORT_ERROR}",
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

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _set_remote(self, remote: str) -> None:
        existing = self._git("remote")
        if "origin" in existing.splitlines():
            self._git("remote", "set-url", "origin", remote)
        else:
            self._git("remote", "add", "origin", remote)

    def test_https_scp_and_embedded_credentials_share_one_safe_identity(self) -> None:
        resolver = RepositoryResolver(timeout_seconds=0.5)
        variants = (
            "https://github.com/OpenAI/example.git",
            "git@github.com:OpenAI/example.git",
            "https://user:password@github.com/OpenAI/example.git",
        )
        snapshots = []
        for remote in variants:
            with self.subTest(remote=remote):
                self._set_remote(remote)
                snapshot = resolver.resolve(self.repository)
                self.assertIsNotNone(snapshot)
                snapshots.append(snapshot)

        self.assertEqual(
            {EXPECTED_REPOSITORY_ID},
            {snapshot.repository_id for snapshot in snapshots},
        )
        self.assertEqual(str(self.repository.resolve()), snapshots[0].worktree_root)
        self.assertEqual("main", snapshots[0].branch)
        self.assertRegex(snapshots[0].head_commit, re.compile(r"^[0-9a-f]{40}$"))
        self.assertNotIn("password", repr(snapshots))

    def test_detached_head_retains_commit_and_uses_no_branch_name(self) -> None:
        self._set_remote("https://github.com/OpenAI/example.git")
        head = self._git("rev-parse", "HEAD")
        self._git("checkout", "--detach", head)

        snapshot = RepositoryResolver(timeout_seconds=0.5).resolve(self.repository)

        self.assertIsNotNone(snapshot)
        self.assertIsNone(snapshot.branch)
        self.assertEqual(head, snapshot.head_commit)

    def test_unicode_or_whitespace_in_remote_is_rejected(self) -> None:
        for remote in (
            "https://github.com/OpenAI/例子.git",
            "https://github.com/OpenAI/example repo.git",
            "git@github.com:OpenAI/example\tcopy.git",
        ):
            with self.subTest(remote=remote):
                self._set_remote(remote)
                self.assertIsNone(
                    RepositoryResolver(timeout_seconds=0.5).resolve(self.repository)
                )

    def test_non_git_directory_and_repository_without_remote_are_ignored(self) -> None:
        non_repository = self.root / "not-git"
        non_repository.mkdir()

        resolver = RepositoryResolver(timeout_seconds=0.5)
        self.assertIsNone(resolver.resolve(non_repository))
        self.assertIsNone(resolver.resolve(self.repository))

    def test_one_total_deadline_bounds_all_git_commands(self) -> None:
        slow_git = self.root / "slow-git"
        slow_git.write_text("#!/bin/sh\nsleep 1\n", "utf-8")
        slow_git.chmod(0o755)
        resolver = RepositoryResolver(
            git_executable=str(slow_git), timeout_seconds=0.02
        )

        started = time.monotonic()
        snapshot = resolver.resolve(self.repository)
        elapsed = time.monotonic() - started

        self.assertIsNone(snapshot)
        self.assertLess(elapsed, 0.3)

    def test_missing_or_relative_cwd_is_rejected_without_running_git(self) -> None:
        missing = self.root / "missing"

        self.assertIsNone(RepositoryResolver().resolve(missing))
        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertIsNone(RepositoryResolver().resolve(Path("repository")))
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
