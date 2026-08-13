from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_nul_delimited_paths_preserve_legal_git_filenames(self) -> None:
        unusual_paths = (
            "packages/shared/theme/src/tab\tname.ts",
            "packages/shared/theme/src/line\nbreak.ts",
            "packages/shared/theme/src/slash\\name.ts",
            "packages/shared/theme/src/你好.ts",
        )
        for path in unusual_paths:
            self._write(path, "path-only evidence\n")

        frozen = self.reader.freeze(self.repository, ())

        for path in unusual_paths:
            with self.subTest(path=path):
                self.assertIn(path, frozen.paths)

    def test_worktree_and_index_diffs_bind_the_frozen_head_commit(self) -> None:
        commands: list[tuple[str, ...]] = []
        path_output = self.reader._path_output

        def record(root: Path, arguments: tuple[str, ...]) -> tuple[str, ...]:
            commands.append(arguments)
            return path_output(root, arguments)

        self.reader._path_output = record

        frozen = self.reader.freeze(self.repository, ())

        self.assertEqual(self.head, frozen.head_commit)
        self.assertIn(self.head, commands[0])
        self.assertIn(self.head, commands[1])
        self.assertNotIn("HEAD", tuple(value for command in commands for value in command))

    def test_git_reads_disable_lazy_fetch_and_external_helpers(self) -> None:
        records_path = self.root / "git-invocations.jsonl"
        wrapper_path = self.root / "recording-git"
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"with open({str(records_path)!r}, 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps({\n"
            "        'argv': sys.argv[1:],\n"
            "        'no_lazy_fetch': os.environ.get('GIT_NO_LAZY_FETCH'),\n"
            "        'terminal_prompt': os.environ.get('GIT_TERMINAL_PROMPT'),\n"
            "    }) + '\\n')\n"
            f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
            "utf-8",
        )
        wrapper_path.chmod(0o700)
        self.reader.git_executable = str(wrapper_path)

        self.reader.freeze(self.repository, ())

        records = tuple(
            json.loads(line) for line in records_path.read_text("utf-8").splitlines()
        )
        self.assertTrue(records)
        self.assertTrue(
            all(record["no_lazy_fetch"] == "1" for record in records)
        )
        self.assertTrue(
            all(record["terminal_prompt"] == "0" for record in records)
        )
        diff_commands = tuple(
            record["argv"] for record in records if "diff" in record["argv"]
        )
        self.assertTrue(diff_commands)
        for command in diff_commands:
            self.assertIn("--no-ext-diff", command)
            self.assertIn("--no-textconv", command)
            self.assertIn("core.fsmonitor=false", command)

    def test_git_output_is_rejected_at_a_fixed_byte_bound(self) -> None:
        executable = self.root / "oversized-git"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdout.buffer.write(b'a' * (9 * 1024 * 1024))\n",
            "utf-8",
        )
        executable.chmod(0o700)
        self.reader.git_executable = str(executable)

        with self.assertRaisesRegex(OSError, "git_path_output_too_large"):
            self.reader.freeze(self.repository, ())

    def test_repository_identity_is_reverified_before_git_reads(self) -> None:
        mismatched = type(self.repository)(
            repository_id="repo_" + "f" * 32,
            worktree_root=self.repository.worktree_root,
            branch=self.repository.branch,
            head_commit=self.repository.head_commit,
        )

        with self.assertRaisesRegex(ValueError, "repository_identity_mismatch"):
            self.reader.freeze(mismatched, ())

    def test_repository_reverification_uses_the_capture_timeout(self) -> None:
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        bin_directory = self.root / "slow-bin"
        bin_directory.mkdir()
        slow_git = bin_directory / "git"
        slow_git.write_text(
            "#!/bin/sh\n"
            "sleep 0.12\n"
            f"exec {real_git!s} \"$@\"\n",
            "utf-8",
        )
        slow_git.chmod(0o700)
        environment = {"PATH": f"{bin_directory}:{os.environ['PATH']}"}

        with patch.dict("os.environ", environment):
            frozen = GitPathEvidenceReader(timeout_seconds=2.0).freeze(
                self.repository, ()
            )

        self.assertEqual(self.repository.repository_id, frozen.repository_id)

    def test_temporarily_unresolved_repository_is_retryable(self) -> None:
        class UnavailableResolver:
            def resolve(self, cwd):
                return None

        reader = GitPathEvidenceReader(
            resolver=UnavailableResolver(), timeout_seconds=0.5
        )

        with self.assertRaisesRegex(OSError, "repository_identity_unavailable"):
            reader.freeze(self.repository, ())


if __name__ == "__main__":
    unittest.main()
