"""Commit-bound, read-only access to the formal Decision Registry."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.catalog import ROOT_PATH
from zdecision.registry.git import GitRegistryAdapter, GitRegistryError
from zdecision.registry.models import (
    DecisionRevision,
    ProductMetadata,
    ProductRegistry,
    RootRegistry,
)


_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RegistrySnapshot:
    commit_sha: str
    products: Mapping[str, ProductMetadata]
    registries: Mapping[str, ProductRegistry]
    decisions: Mapping[tuple[str, str], DecisionRevision]

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", MappingProxyType(dict(self.products)))
        object.__setattr__(
            self, "registries", MappingProxyType(dict(self.registries))
        )
        object.__setattr__(
            self, "decisions", MappingProxyType(dict(self.decisions))
        )


class RegistryQueryUnavailable(Exception):
    code = "registry_unavailable"


class RegistryQuery:
    def __init__(self, repository_root: Path, git: GitRegistryAdapter) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.git = git

    def snapshot(self) -> RegistrySnapshot:
        try:
            commit_sha = self.git.fetch_and_require_exact_main()
            root = RootRegistry.from_dict(self._read(commit_sha, ROOT_PATH))
            products: dict[str, ProductMetadata] = {}
            registries: dict[str, ProductRegistry] = {}
            decisions: dict[tuple[str, str], DecisionRevision] = {}
            for product_id, entry in root.products.items():
                metadata = ProductMetadata.from_dict(
                    self._read(
                        commit_sha,
                        f"decision-registry/{entry.product_path}",
                    )
                )
                registry = ProductRegistry.from_dict(
                    self._read(
                        commit_sha,
                        f"decision-registry/{entry.registry_path}",
                    )
                )
                if (
                    metadata.product_id != product_id
                    or metadata.name != entry.name
                    or registry.product_id != product_id
                ):
                    raise ValueError("Registry product ownership mismatch")
                products[product_id] = metadata
                registries[product_id] = registry
                for decision_id, head in registry.decisions.items():
                    relative = (
                        f"decision-registry/products/{product_id}/"
                        f"{head.head_path}"
                    )
                    revision = DecisionRevision.from_dict(
                        self._read(commit_sha, relative)
                    )
                    if (
                        revision.product_id != product_id
                        or revision.product_name != metadata.name
                        or revision.decision_id != decision_id
                        or revision.revision != head.head_revision
                        or revision.lifecycle != head.lifecycle
                    ):
                        raise ValueError("Registry Decision ownership mismatch")
                    decisions[(product_id, decision_id)] = revision
            if (
                self.git.fetch_and_require_exact_main(
                    expected_base=commit_sha
                )
                != commit_sha
            ):
                raise ValueError("Registry commit changed during the read")
            return RegistrySnapshot(
                commit_sha, products, registries, decisions
            )
        except RegistryQueryUnavailable:
            raise
        except (GitRegistryError, OSError, UnicodeError, TypeError, ValueError):
            raise RegistryQueryUnavailable("registry_unavailable") from None

    def _read(
        self, commit_sha: str, relative_path: str
    ) -> Mapping[str, object]:
        if (
            not isinstance(commit_sha, str)
            or _COMMIT.fullmatch(commit_sha) is None
        ):
            raise ValueError("Registry commit is invalid")
        pure = PurePosixPath(relative_path)
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.parts[0] != "decision-registry"
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            raise ValueError("Registry path is invalid")
        tree_entry = self._git(
            "git",
            "--no-replace-objects",
            "ls-tree",
            "-z",
            "--full-tree",
            commit_sha,
            "--",
            relative_path,
        )
        if tree_entry.count(b"\0") != 1 or not tree_entry.endswith(b"\0"):
            raise ValueError("Registry document is unavailable")
        try:
            metadata, found_path = tree_entry[:-1].split(b"\t", 1)
            mode, object_type, blob_sha = metadata.split(b" ")
            decoded_path = found_path.decode("utf-8")
            decoded_blob = blob_sha.decode("ascii")
        except (UnicodeDecodeError, ValueError):
            raise ValueError("Registry Git entry is invalid") from None
        if (
            mode not in (b"100644", b"100755")
            or object_type != b"blob"
            or _COMMIT.fullmatch(decoded_blob) is None
            or decoded_path != relative_path
        ):
            raise ValueError("Registry Git entry is invalid")
        raw = self._git(
            "git", "--no-replace-objects", "cat-file", "blob", decoded_blob
        )
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _: self._reject_constant(),
        )
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise ValueError("Registry document is not canonical JSON")
        return value

    def _git(self, *command: str) -> bytes:
        try:
            result = subprocess.run(
                command,
                cwd=self.repository_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise OSError("Registry Git object is unavailable") from None
        if result.returncode != 0:
            raise OSError("Registry Git object is unavailable")
        return result.stdout

    @staticmethod
    def _reject_constant() -> None:
        raise ValueError("Registry document contains a JSON constant")
