"""Credential-free Git repository identification under one short deadline."""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from zdecision.agent.events import RepositorySnapshot


_SCP_REMOTE = re.compile(r"^(?:[^@/:]+@)?([^/:]+):(.+)$")
_HEAD = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class RepositoryResolver:
    """Resolve safe repository facts without retaining a remote URL."""

    def __init__(
        self,
        *,
        git_executable: str = "git",
        timeout_seconds: float = 0.08,
    ) -> None:
        self.git_executable = git_executable
        self.timeout_seconds = timeout_seconds

    def resolve(self, cwd: str | Path) -> RepositorySnapshot | None:
        path = Path(cwd)
        if not path.is_absolute() or not path.is_dir():
            return None
        deadline = time.monotonic() + self.timeout_seconds
        try:
            metadata = self._run(
                path,
                ("rev-parse", "--show-toplevel", "--absolute-git-dir", "HEAD"),
                deadline,
            )
            remote = self._run(
                path,
                ("config", "--get", "remote.origin.url"),
                deadline,
            )
            if metadata is None:
                return None
            lines = metadata.splitlines()
            if len(lines) != 3:
                return None
            root, git_directory, head = lines
            branch = self._read_branch(Path(git_directory), deadline)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        if root is None or remote is None or head is None:
            return None
        root_path = Path(root)
        if not root_path.is_absolute() or not root_path.is_dir():
            return None
        canonical_remote = normalize_remote(remote)
        if canonical_remote is None or _HEAD.fullmatch(head) is None:
            return None
        if branch is not None and not _safe_branch(branch):
            return None
        digest = hashlib.sha256(canonical_remote.encode("utf-8")).hexdigest()
        return RepositorySnapshot(
            repository_id=f"repo_{digest[:32]}",
            worktree_root=str(root_path.resolve()),
            branch=branch,
            head_commit=head,
        )

    def _read_branch(self, git_directory: Path, deadline: float) -> str | None:
        if time.monotonic() >= deadline or not git_directory.is_absolute():
            raise subprocess.TimeoutExpired(self.git_executable, self.timeout_seconds)
        with (git_directory / "HEAD").open("rb") as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise ValueError("Git HEAD is too large")
        text = raw.decode("utf-8").strip()
        prefix = "ref: refs/heads/"
        if text.startswith(prefix):
            return text.removeprefix(prefix)
        if _HEAD.fullmatch(text) is not None:
            return None
        raise ValueError("Git HEAD is invalid")

    def _run(
        self,
        cwd: Path,
        arguments: tuple[str, ...],
        deadline: float,
        *,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> str | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(self.git_executable, self.timeout_seconds)
        result = subprocess.run(
            [self.git_executable, *arguments],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=remaining,
        )
        if result.returncode not in allowed_returncodes:
            raise ValueError("Git command failed")
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None


def normalize_remote(remote: str) -> str | None:
    """Normalize supported Git remotes without retaining credentials."""

    if not isinstance(remote, str) or not 1 <= len(remote) <= 2048:
        return None
    if not remote.isascii() or any(character.isspace() for character in remote):
        return None
    host: str | None = None
    raw_path: str | None = None
    if remote.startswith("https://"):
        try:
            parsed = urlsplit(remote)
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme != "https" or parsed.hostname is None:
            return None
        if parsed.query or parsed.fragment:
            return None
        host = parsed.hostname.lower()
        if port is not None and port != 443:
            host = f"{host}:{port}"
        raw_path = parsed.path
    else:
        match = _SCP_REMOTE.fullmatch(remote)
        if match is None:
            return None
        host = match.group(1).lower()
        raw_path = match.group(2)
    path = unquote(raw_path).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not _safe_remote_component(host) or not _safe_remote_component(path):
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return f"{host}/{path}"


def _safe_remote_component(value: str) -> bool:
    return bool(
        value
        and value.isascii()
        and not any(character.isspace() or ord(character) < 33 for character in value)
    )


def _safe_branch(value: str) -> bool:
    return bool(
        1 <= len(value) <= 512
        and "\x00" not in value
        and not any(ord(character) < 32 for character in value)
    )
