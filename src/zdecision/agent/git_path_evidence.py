"""Private, path-only Git evidence for local Capture routing."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from zdecision.agent.events import RepositorySnapshot
from zdecision.agent.repository import RepositoryResolver
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.jsonio import canonical_json_bytes


_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_STDOUT_BYTES = 8 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024


@dataclass(frozen=True)
class FrozenCommitRange:
    source_key: str
    base_exclusive: str
    head_inclusive: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, str) or not self.source_key:
            raise ValueError("source_key is invalid")
        for field_name in ("base_exclusive", "head_inclusive"):
            if _COMMIT.fullmatch(getattr(self, field_name)) is None:
                raise ValueError(f"{field_name} is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "source_key": self.source_key,
            "base_exclusive": self.base_exclusive,
            "head_inclusive": self.head_inclusive,
        }


@dataclass(frozen=True)
class FrozenGitPathEvidence:
    repository_id: str
    head_commit: str
    commit_ranges: tuple[FrozenCommitRange, ...]
    paths: tuple[str, ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if _REPOSITORY_ID.fullmatch(self.repository_id) is None:
            raise ValueError("repository_id is invalid")
        if _COMMIT.fullmatch(self.head_commit) is None:
            raise ValueError("head_commit is invalid")
        if (
            not isinstance(self.commit_ranges, tuple)
            or any(
                not isinstance(item, FrozenCommitRange)
                for item in self.commit_ranges
            )
        ):
            raise ValueError("commit_ranges are invalid")
        if self.paths != tuple(sorted(set(self.paths))):
            raise ValueError("paths are not normalized")
        for path in self.paths:
            _normalized_path(path)
        if _DIGEST.fullmatch(self.evidence_digest) is None:
            raise ValueError("evidence_digest is invalid")
        if self.evidence_digest != _evidence_digest(
            self.repository_id,
            self.head_commit,
            self.commit_ranges,
            self.paths,
        ):
            raise ValueError("evidence_digest does not match evidence")

    @classmethod
    def create(
        cls,
        *,
        repository_id: str,
        head_commit: str,
        commit_ranges: tuple[FrozenCommitRange, ...],
        paths: tuple[str, ...],
    ) -> "FrozenGitPathEvidence":
        ordered_ranges = tuple(
            sorted(
                commit_ranges,
                key=lambda item: (
                    item.source_key,
                    item.base_exclusive,
                    item.head_inclusive,
                ),
            )
        )
        ordered_paths = tuple(sorted({_normalized_path(path) for path in paths}))
        return cls(
            repository_id=repository_id,
            head_commit=head_commit,
            commit_ranges=ordered_ranges,
            paths=ordered_paths,
            evidence_digest=_evidence_digest(
                repository_id,
                head_commit,
                ordered_ranges,
                ordered_paths,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "head_commit": self.head_commit,
            "commit_ranges": [
                item.to_dict() for item in self.commit_ranges
            ],
            "paths": list(self.paths),
            "evidence_digest": self.evidence_digest,
        }


class GitPathEvidenceReader:
    """Freeze normalized path names without reading source or diff content."""

    def __init__(
        self,
        *,
        resolver: RepositoryResolver | None = None,
        git_executable: str = "git",
        timeout_seconds: float = 2.0,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds is invalid")
        self.resolver = resolver or RepositoryResolver()
        self.git_executable = git_executable
        self.timeout_seconds = float(timeout_seconds)

    def freeze(
        self,
        repository: RepositorySnapshot,
        sources: tuple[FrozenSessionSource, ...],
    ) -> FrozenGitPathEvidence:
        if not isinstance(repository, RepositorySnapshot):
            raise TypeError("repository must be a RepositorySnapshot")
        if (
            not isinstance(sources, tuple)
            or any(not isinstance(source, FrozenSessionSource) for source in sources)
        ):
            raise TypeError("sources must contain FrozenSessionSource values")
        resolved = self.resolver.resolve(repository.worktree_root)
        if (
            resolved is None
            or resolved.repository_id != repository.repository_id
            or Path(resolved.worktree_root) != Path(repository.worktree_root)
        ):
            raise ValueError("repository_identity_mismatch")
        if any(source.repository_id != resolved.repository_id for source in sources):
            raise ValueError("source_repository_mismatch")
        root = Path(resolved.worktree_root)
        paths: set[str] = set()
        for arguments in (
            (
                "diff",
                "--name-only",
                "-z",
                "--diff-filter=ACMRTUXB",
                resolved.head_commit,
            ),
            (
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--diff-filter=ACMRTUXB",
                resolved.head_commit,
            ),
            ("ls-files", "-z", "--others", "--exclude-standard"),
        ):
            paths.update(self._path_output(root, arguments))

        ranges: list[FrozenCommitRange] = []
        seen_ranges: set[tuple[str, str]] = set()
        for source in sources:
            base = source.previous_handled_head_commit
            head = source.upper_head_commit
            if (
                base is None
                or head is None
                or _COMMIT.fullmatch(base) is None
                or _COMMIT.fullmatch(head) is None
                or not self._is_commit(root, base)
                or not self._is_commit(root, head)
            ):
                continue
            coordinates = (base, head)
            if coordinates in seen_ranges:
                continue
            seen_ranges.add(coordinates)
            paths.update(
                self._path_output(
                    root,
                    (
                        "diff",
                        "--name-only",
                        "-z",
                        "--diff-filter=ACMRTUXB",
                        f"{base}..{head}",
                    ),
                )
            )
            ranges.append(FrozenCommitRange(source.source_key, base, head))
        return FrozenGitPathEvidence.create(
            repository_id=resolved.repository_id,
            head_commit=resolved.head_commit,
            commit_ranges=tuple(ranges),
            paths=tuple(paths),
        )

    def _is_commit(self, root: Path, coordinate: str) -> bool:
        result = self._run(root, ("cat-file", "-e", f"{coordinate}^{{commit}}"))
        return result.returncode == 0

    def _path_output(
        self, root: Path, arguments: tuple[str, ...]
    ) -> tuple[str, ...]:
        result = self._run(root, arguments)
        if result.returncode != 0:
            raise OSError("git_path_command_failed")
        if result.stdout and not result.stdout.endswith(b"\x00"):
            raise ValueError("git_path_invalid")
        try:
            return tuple(
                _normalized_path(encoded.decode("utf-8", errors="strict"))
                for encoded in result.stdout.split(b"\x00")
                if encoded
            )
        except UnicodeError as error:
            raise ValueError("git_path_invalid") from error

    def _run(
        self, root: Path, arguments: tuple[str, ...]
    ) -> subprocess.CompletedProcess[bytes]:
        command_arguments = list(arguments)
        if command_arguments[0] == "diff":
            command_arguments[1:1] = ["--no-ext-diff", "--no-textconv"]
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        command = [
            self.git_executable,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "diff.external=",
            *command_arguments,
        ]
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        output: list[bytes] = []
        errors: list[bytes] = []
        readers = (
            threading.Thread(
                target=_read_bounded,
                args=(process.stdout, _MAX_STDOUT_BYTES, output),
                daemon=True,
            ),
            threading.Thread(
                target=_read_bounded,
                args=(process.stderr, _MAX_STDERR_BYTES, errors),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for reader in readers:
                reader.join()
            process.stdout.close()
            process.stderr.close()
            raise OSError("git_path_command_failed") from error
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()
        stdout = output[0]
        stderr = errors[0]
        if (
            len(stdout) > _MAX_STDOUT_BYTES
            or len(stderr) > _MAX_STDERR_BYTES
        ):
            raise OSError("git_path_output_too_large")
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout,
            stderr,
        )


def _normalized_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or value.startswith("/")
    ):
        raise ValueError("git_path_invalid")
    path = PurePosixPath(value)
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("git_path_invalid")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError("git_path_invalid")
    return normalized


def _read_bounded(stream, limit: int, target: list[bytes]) -> None:
    buffered = bytearray()
    while chunk := stream.read(64 * 1024):
        remaining = limit + 1 - len(buffered)
        if remaining > 0:
            buffered.extend(chunk[:remaining])
    target.append(bytes(buffered))


def _evidence_digest(
    repository_id: str,
    head_commit: str,
    commit_ranges: tuple[FrozenCommitRange, ...],
    paths: tuple[str, ...],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "repository_id": repository_id,
                "head_commit": head_commit,
                "commit_ranges": [item.to_dict() for item in commit_ranges],
                "paths": list(paths),
            }
        )
    ).hexdigest()
