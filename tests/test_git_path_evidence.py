from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from zdecision.agent.git_path_evidence import GitPathEvidenceReader
from zdecision.agent.repository import RepositoryResolver
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.jsonio import canonical_json_bytes


REQUEST_ID = "crq_" + "1" * 32


class GitPathEvidenceReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test User")
        self.git(
            "remote",
            "add",
            "origin",
            "https://example.test/acme/monorepo.git",
        )
        self._write("README.md", "initial\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "initial")
        self.base = self.git("rev-parse", "HEAD")
        self._write(
            "packages/products/cloud/apps/core-shell/src/app.tsx",
            "committed change\n",
        )
        self.git("add", ".")
        self.git("commit", "-m", "cloud change")
        self.head = self.git("rev-parse", "HEAD")
        self._write(
            "packages/shared/theme/src/index.ts",
            "PRIVATE_SOURCE_SENTINEL\n",
        )
        self._write(
            "packages/products/shared/zcf-license/src/App.tsx",
            "staged private value\n",
        )
        self.git(
            "add", "packages/products/shared/zcf-license/src/App.tsx"
        )
        resolver = RepositoryResolver(timeout_seconds=2.0)
        self.repository = resolver.resolve(self.root)
        self.assertIsNotNone(self.repository)
        self.reader = GitPathEvidenceReader(
            resolver=resolver, timeout_seconds=2.0
        )

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")

    def source(
        self,
        *,
        previous: str | None = None,
        upper: str | None = None,
    ) -> FrozenSessionSource:
        return FrozenSessionSource(
            request_id=REQUEST_ID,
            source_key="src_" + "2" * 32,
            repository_id=self.repository.repository_id,
            session_id="019fb100-0000-7000-8000-000000000001",
            cwd=str(self.root),
            lineage="lin_" + "3" * 32,
            previous_handled_turn_id=None,
            upper_turn_id="019fb100-0000-7000-8000-000000000002",
            source_fingerprint="4" * 64,
            previous_handled_head_commit=previous,
            upper_head_commit=upper,
        )

    def test_freezes_committed_index_worktree_and_untracked_paths_only(
        self,
    ) -> None:
        frozen = self.reader.freeze(
            self.repository,
            (self.source(previous=self.base, upper=self.head),),
        )

        self.assertEqual(
            (
                "packages/products/cloud/apps/core-shell/src/app.tsx",
                "packages/products/shared/zcf-license/src/App.tsx",
                "packages/shared/theme/src/index.ts",
            ),
            frozen.paths,
        )
        self.assertEqual(1, len(frozen.commit_ranges))
        encoded = canonical_json_bytes(frozen.to_dict())
        self.assertIn(b"packages/shared/theme/src/index.ts", encoded)
        self.assertNotIn(b"PRIVATE_SOURCE_SENTINEL", encoded)
        self.assertNotIn(b"session_id", encoded)
        self.assertNotIn(str(self.root).encode(), encoded)

    def test_invalid_commit_coordinates_omit_range_without_guessing(self) -> None:
        frozen = self.reader.freeze(
            self.repository,
            (self.source(previous="0" * 40, upper=self.head),),
        )

        self.assertEqual((), frozen.commit_ranges)
        self.assertNotIn(
            "packages/products/cloud/apps/core-shell/src/app.tsx",
            frozen.paths,
        )
        self.assertIn("packages/shared/theme/src/index.ts", frozen.paths)

    def test_repository_identity_is_reverified_before_git_reads(self) -> None:
        mismatched = type(self.repository)(
            repository_id="repo_" + "f" * 32,
            worktree_root=self.repository.worktree_root,
            branch=self.repository.branch,
            head_commit=self.repository.head_commit,
        )

        with self.assertRaisesRegex(ValueError, "repository_identity_mismatch"):
            self.reader.freeze(mismatched, ())


if __name__ == "__main__":
    unittest.main()
