"""Commit-bound, read-only access to the formal Decision Registry."""

from __future__ import annotations

import json
import os
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
            self.git.require_clean_registry()
            root = RootRegistry.from_dict(self._read(ROOT_PATH))
            products: dict[str, ProductMetadata] = {}
            registries: dict[str, ProductRegistry] = {}
            decisions: dict[tuple[str, str], DecisionRevision] = {}
            for product_id, entry in root.products.items():
                metadata = ProductMetadata.from_dict(
                    self._read(f"decision-registry/{entry.product_path}")
                )
                registry = ProductRegistry.from_dict(
                    self._read(f"decision-registry/{entry.registry_path}")
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
                    revision = DecisionRevision.from_dict(self._read(relative))
                    if (
                        revision.product_id != product_id
                        or revision.product_name != metadata.name
                        or revision.decision_id != decision_id
                        or revision.revision != head.head_revision
                        or revision.lifecycle != head.lifecycle
                    ):
                        raise ValueError("Registry Decision ownership mismatch")
                    decisions[(product_id, decision_id)] = revision
            self.git.require_clean_registry()
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

    def _read(self, relative_path: str) -> Mapping[str, object]:
        pure = PurePosixPath(relative_path)
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.parts[0] != "decision-registry"
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            raise ValueError("Registry path is invalid")
        path = self.repository_root.joinpath(*pure.parts)
        registry_root = self.repository_root / "decision-registry"
        path.relative_to(registry_root)
        current = registry_root
        if current.is_symlink():
            raise ValueError("Registry path contains a symlink")
        for part in path.relative_to(registry_root).parts:
            current = current / part
            if os.path.lexists(current) and current.is_symlink():
                raise ValueError("Registry path contains a symlink")
        if not path.is_file():
            raise OSError("Registry document is unavailable")
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _: self._reject_constant(),
        )
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise ValueError("Registry document is not canonical JSON")
        return value

    @staticmethod
    def _reject_constant() -> None:
        raise ValueError("Registry document contains a JSON constant")
