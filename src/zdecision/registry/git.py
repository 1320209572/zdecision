"""Safe Git adapter for exact Decision Registry publication commits."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


CANONICAL_ORIGIN_URL = "https://github.com/1320209572/zdecision.git"

_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class GitRegistryError(Exception):
    """Base class for sanitized Git Registry failures."""


class RegistryOutOfSync(GitRegistryError):
    pass


class RegistryGitConflict(GitRegistryError):
    pass


class PublicationGitAmbiguous(GitRegistryError):
    pass


class RegistryPushFailed(GitRegistryError):
    pass


@dataclass(frozen=True)
class ReconciledCommit:
    commit_sha: str
    remote_contains_commit: bool


class GitRegistryAdapter:
    def __init__(
        self,
        repository_root: Path,
        expected_origin: str = CANONICAL_ORIGIN_URL,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        if not isinstance(expected_origin, str) or not expected_origin:
            raise ValueError("Expected origin must not be empty")
        self.expected_origin = expected_origin

    def fetch_and_require_exact_main(self, expected_base: str | None = None) -> str:
        self._require_origin_and_main(fetch=True)
        head = self._revision("HEAD", RegistryOutOfSync)
        local_main = self._revision("refs/heads/main", RegistryOutOfSync)
        remote_main = self._revision(
            "refs/remotes/origin/main",
            RegistryOutOfSync,
        )
        if head != local_main or head != remote_main:
            raise RegistryOutOfSync("Local main is not exactly synchronized")
        if expected_base is not None:
            self._validated_commit(expected_base, "Expected base")
            if head != expected_base:
                raise RegistryOutOfSync("Local main no longer matches the preview base")
        return head

    def require_clean_registry(
        self,
        allowed_exact_files: Mapping[str, bytes] | None = None,
    ) -> None:
        allowed = self._validated_files(allowed_exact_files or {})
        result = self._run(
            (
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                "decision-registry",
            ),
            error_type=RegistryGitConflict,
        )
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            if len(record) < 4:
                raise RegistryGitConflict("Registry Git status is invalid")
            status = record[:2]
            if b"R" in status or b"C" in status:
                raise RegistryGitConflict("Registry contains an unowned change")
            try:
                path = record[3:].decode("utf-8")
            except UnicodeDecodeError:
                raise RegistryGitConflict("Registry contains an invalid path") from None
            expected = allowed.get(path)
            if expected is None or not self._worktree_file_equals(path, expected):
                raise RegistryGitConflict("Registry contains an unowned change")

    def commit_exact(
        self,
        base_commit: str,
        message: str,
        changed_files: Mapping[str, bytes],
    ) -> str:
        self._validated_commit(base_commit, "Publication base")
        self._validated_message(message)
        files = self._validated_files(changed_files)
        self._require_origin_and_main(fetch=False)
        if self._revision("HEAD", RegistryGitConflict) != base_commit:
            raise RegistryGitConflict("Publication base is no longer HEAD")
        self.require_clean_registry(files)
        self._require_exact_worktree_files(files)
        paths = tuple(sorted(files))
        self._run(
            ("git", "add", "--", *paths),
            error_type=RegistryGitConflict,
        )
        commit = self._run(
            (
                "git",
                "commit",
                "--only",
                "--cleanup=verbatim",
                "--file=-",
                "--",
                *paths,
            ),
            input_bytes=message.encode("utf-8"),
            check=False,
        )
        head = self._revision("HEAD", PublicationGitAmbiguous)
        if commit.returncode != 0 and head == base_commit:
            raise RegistryGitConflict("Registry publication commit failed")
        self._verify_exact_commit(head, base_commit, message, files)
        return head

    def reconcile_exact_commit(
        self,
        base_commit: str,
        message: str,
        changed_files: Mapping[str, bytes],
    ) -> ReconciledCommit | None:
        self._validated_commit(base_commit, "Publication base")
        self._validated_message(message)
        files = self._validated_files(changed_files)
        self._require_origin_and_main(fetch=True)
        head = self._revision("HEAD", PublicationGitAmbiguous)
        remote = self._revision(
            "refs/remotes/origin/main",
            PublicationGitAmbiguous,
        )
        if head == base_commit:
            if remote == base_commit:
                return None
            raise PublicationGitAmbiguous("Publication Git state is ambiguous")
        self._verify_exact_commit(head, base_commit, message, files)
        if self._is_ancestor(head, remote):
            return ReconciledCommit(head, True)
        if remote == base_commit:
            return ReconciledCommit(head, False)
        raise PublicationGitAmbiguous("Publication Git state is ambiguous")

    def publication_remote_state(
        self,
        commit_sha: str,
        base_commit: str,
    ) -> str:
        """Return ``contains`` or ``base`` for the only safe remote states."""

        self._validated_commit(commit_sha, "Publication commit")
        self._validated_commit(base_commit, "Publication base")
        self._require_origin_and_main(fetch=True)
        remote = self._revision(
            "refs/remotes/origin/main",
            PublicationGitAmbiguous,
        )
        if self._is_ancestor(commit_sha, remote):
            return "contains"
        if remote == base_commit and self._commit_parents(commit_sha) == (
            base_commit,
        ):
            return "base"
        raise PublicationGitAmbiguous("Publication remote state is ambiguous")

    def push_exact(self, commit_sha: str, base_commit: str) -> None:
        self._validated_commit(commit_sha, "Publication commit")
        self._validated_commit(base_commit, "Publication base")
        self._require_origin_and_main(fetch=True)
        remote = self._revision(
            "refs/remotes/origin/main",
            PublicationGitAmbiguous,
        )
        if self._is_ancestor(commit_sha, remote):
            return
        if remote != base_commit:
            raise PublicationGitAmbiguous("Remote main has an unrelated state")
        parents = self._commit_parents(commit_sha)
        if parents != (base_commit,):
            raise PublicationGitAmbiguous("Publication commit parent is ambiguous")
        self._run(
            (
                "git",
                "push",
                "origin",
                f"{commit_sha}:refs/heads/main",
            ),
            check=False,
        )
        try:
            self._fetch_main()
        except RegistryOutOfSync:
            raise RegistryPushFailed("Publication push could not be verified") from None
        refreshed_remote = self._revision(
            "refs/remotes/origin/main",
            RegistryPushFailed,
        )
        if self._is_ancestor(commit_sha, refreshed_remote):
            return
        raise RegistryPushFailed("Publication push could not be verified")

    def _require_origin_and_main(self, *, fetch: bool) -> None:
        if not self.repository_root.is_dir():
            raise RegistryOutOfSync("Repository is unavailable")
        fetch_url = self._run(
            ("git", "remote", "get-url", "origin"),
            error_type=RegistryOutOfSync,
        ).stdout.decode("utf-8", errors="replace").strip()
        push_url = self._run(
            ("git", "remote", "get-url", "--push", "origin"),
            error_type=RegistryOutOfSync,
        ).stdout.decode("utf-8", errors="replace").strip()
        if fetch_url != self.expected_origin or push_url != self.expected_origin:
            raise RegistryOutOfSync("Repository origin is not canonical")
        branch = self._run(
            ("git", "symbolic-ref", "--quiet", "--short", "HEAD"),
            check=False,
        )
        if branch.returncode != 0 or branch.stdout.strip() != b"main":
            raise RegistryOutOfSync("Repository is not on branch main")
        if fetch:
            self._fetch_main()

    def _fetch_main(self) -> None:
        result = self._run(
            (
                "git",
                "fetch",
                "--no-tags",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ),
            check=False,
        )
        if result.returncode != 0:
            raise RegistryOutOfSync("Unable to refresh origin/main")

    def _verify_exact_commit(
        self,
        commit_sha: str,
        base_commit: str,
        message: str,
        files: Mapping[str, bytes],
    ) -> None:
        self._validated_commit(commit_sha, "Publication commit")
        if self._commit_parents(commit_sha) != (base_commit,):
            raise PublicationGitAmbiguous("Publication commit parent is ambiguous")
        raw_commit = self._run(
            ("git", "cat-file", "commit", commit_sha),
            error_type=PublicationGitAmbiguous,
        ).stdout
        separator = raw_commit.find(b"\n\n")
        if separator < 0 or raw_commit[separator + 2 :] != message.encode("utf-8"):
            raise PublicationGitAmbiguous("Publication commit message is ambiguous")
        changed_result = self._run(
            (
                "git",
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit_sha,
            ),
            error_type=PublicationGitAmbiguous,
        )
        try:
            changed = {
                path.decode("utf-8")
                for path in changed_result.stdout.split(b"\0")
                if path
            }
        except UnicodeDecodeError:
            raise PublicationGitAmbiguous("Publication commit path is ambiguous") from None
        if changed != set(files):
            raise PublicationGitAmbiguous("Publication commit paths are ambiguous")
        for path, expected in files.items():
            blob = self._run(
                ("git", "show", f"{commit_sha}:{path}"),
                error_type=PublicationGitAmbiguous,
            ).stdout
            if blob != expected:
                raise PublicationGitAmbiguous("Publication commit blob is ambiguous")

    def _commit_parents(self, commit_sha: str) -> tuple[str, ...]:
        result = self._run(
            ("git", "rev-list", "--parents", "-n", "1", commit_sha),
            error_type=PublicationGitAmbiguous,
        )
        values = result.stdout.decode("ascii", errors="replace").split()
        if not values or values[0] != commit_sha:
            raise PublicationGitAmbiguous("Publication commit identity is ambiguous")
        return tuple(values[1:])

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run(
            ("git", "merge-base", "--is-ancestor", ancestor, descendant),
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise PublicationGitAmbiguous("Unable to compare publication commits")

    def _revision(self, name: str, error_type: type[GitRegistryError]) -> str:
        result = self._run(
            ("git", "rev-parse", "--verify", name),
            error_type=error_type,
        )
        value = result.stdout.decode("ascii", errors="replace").strip()
        if _COMMIT.fullmatch(value) is None:
            raise error_type("Git revision is invalid")
        return value

    def _validated_files(
        self,
        files: Mapping[str, bytes],
    ) -> dict[str, bytes]:
        if not isinstance(files, Mapping):
            raise RegistryGitConflict("Registry file set is invalid")
        validated: dict[str, bytes] = {}
        for path, content in files.items():
            if not isinstance(path, str) or not self._owned_path(path):
                raise RegistryGitConflict("Registry file path is invalid")
            if not isinstance(content, bytes):
                raise RegistryGitConflict("Registry file content is invalid")
            validated[path] = content
        return validated

    @staticmethod
    def _owned_path(path: str) -> bool:
        pure = PurePosixPath(path)
        return (
            not pure.is_absolute()
            and len(pure.parts) >= 2
            and pure.parts[0] == "decision-registry"
            and all(part not in ("", ".", "..") for part in pure.parts)
        )

    def _require_exact_worktree_files(self, files: Mapping[str, bytes]) -> None:
        for path, expected in files.items():
            if not self._worktree_file_equals(path, expected):
                raise RegistryGitConflict("Registry target bytes do not match preview")

    def _worktree_file_equals(self, relative_path: str, expected: bytes) -> bool:
        path = self.repository_root.joinpath(*PurePosixPath(relative_path).parts)
        return (
            os.path.lexists(path)
            and path.is_file()
            and not path.is_symlink()
            and path.read_bytes() == expected
        )

    @staticmethod
    def _validated_commit(value: str, field_name: str) -> None:
        if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
            raise ValueError(f"{field_name} is invalid")

    @staticmethod
    def _validated_message(message: str) -> None:
        if not isinstance(message, str) or not message.endswith("\n"):
            raise ValueError("Publication commit message is invalid")

    def _run(
        self,
        command: tuple[str, ...],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
        error_type: type[GitRegistryError] = RegistryGitConflict,
    ) -> subprocess.CompletedProcess[bytes]:
        if not command or command[0] != "git":
            raise error_type("Git operation is invalid")
        safe_command = (
            "git",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *command[1:],
        )
        environment = os.environ.copy()
        credential_environment = {
            name: environment[name]
            for name in ("GIT_ASKPASS", "GIT_TERMINAL_PROMPT")
            if name in environment
        }
        for name in tuple(environment):
            if name.startswith("GIT_"):
                environment.pop(name)
        environment.update(credential_environment)
        try:
            result = subprocess.run(
                safe_command,
                cwd=self.repository_root,
                env=environment,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise error_type("Git operation is unavailable") from None
        if check and result.returncode != 0:
            raise error_type("Git operation failed")
        return result
