from __future__ import annotations

import unittest
from unittest import mock

from zdecision.central.cli import _synchronize_registry_on_startup
from zdecision.registry.git import RegistryOutOfSync


class _StartupGit:
    def __init__(
        self,
        commit: str = "a" * 40,
        error: Exception | None = None,
    ) -> None:
        self.commit = commit
        self.error = error
        self.fetch_count = 0

    def fetch_and_require_exact_main(self) -> str:
        self.fetch_count += 1
        if self.error is not None:
            raise self.error
        return self.commit


class CentralCliStartupTest(unittest.TestCase):
    def test_startup_fetches_once_and_synchronizes_the_exact_commit(self) -> None:
        git = _StartupGit()
        synchronizer = mock.Mock()
        projection = mock.Mock()

        _synchronize_registry_on_startup(
            "org_demo",
            git,
            synchronizer,
            projection,
            "2026-08-06T10:00:00Z",
        )

        self.assertEqual(1, git.fetch_count)
        synchronizer.synchronize.assert_called_once_with(
            "org_demo", "a" * 40, "2026-08-06T10:00:00Z"
        )
        projection.mark_unavailable.assert_not_called()

    def test_startup_verification_failure_disables_only_formal_reads(self) -> None:
        git = _StartupGit(error=RegistryOutOfSync("offline"))
        synchronizer = mock.Mock()
        projection = mock.Mock()

        _synchronize_registry_on_startup(
            "org_demo",
            git,
            synchronizer,
            projection,
            "2026-08-06T10:00:00Z",
        )

        synchronizer.synchronize.assert_not_called()
        projection.mark_unavailable.assert_called_once_with(
            "org_demo",
            None,
            None,
            None,
            "2026-08-06T10:00:00Z",
            "git_proof_failed",
        )


if __name__ == "__main__":
    unittest.main()
