"""Strict product-isolated Registry loading, planning, and exact writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from zdecision.jsonio import atomic_write_bytes, canonical_json_bytes
from zdecision.registry.models import (
    DecisionHead,
    DecisionRevision,
    DecisionSeed,
    ProductMetadata,
    ProductRegistry,
    RootProductEntry,
    RootRegistry,
)


ROOT_PATH = "decision-registry/registry.json"
_PREVIEW_ID = re.compile(r"^pub_[0-9a-f]{32}$")
_PRODUCT_PATH = re.compile(
    r"^decision-registry/products/(prod_[0-9a-f]{32})/product\.json$"
)
_PRODUCT_REGISTRY_PATH = re.compile(
    r"^decision-registry/products/(prod_[0-9a-f]{32})/registry\.json$"
)
_DECISION_PATH = re.compile(
    r"^decision-registry/products/(prod_[0-9a-f]{32})/"
    r"decisions/(dec_[0-9a-f]{32})/r0001\.json$"
)


class RegistryError(Exception):
    """Base class for sanitized Registry failures."""


class RegistryInvalid(RegistryError):
    pass


class RegistryStale(RegistryError):
    pass


class RegistryConflict(RegistryError):
    pass


class DecisionUpdateNotSupported(RegistryError):
    pass


@dataclass(frozen=True)
class RegistryPlan:
    product_id: str
    product_name: str
    seeds: tuple[DecisionSeed, ...]
    decision_ids: tuple[str, ...]
    decision_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    base_registry_digests: Mapping[str, str]
    current_root: RootRegistry
    next_root: RootRegistry
    product_metadata: ProductMetadata
    current_product_registry: ProductRegistry | None
    next_product_registry: ProductRegistry


@dataclass(frozen=True)
class RegistryDraft:
    display_documents: Mapping[str, bytes]
    changed_files: Mapping[str, bytes]


@dataclass(frozen=True)
class _RegistryState:
    root: RootRegistry
    products: Mapping[str, ProductMetadata]
    registries: Mapping[str, ProductRegistry]


class RegistryCatalog:
    def __init__(self, repository_root: Path) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.registry_root = self.repository_root / "decision-registry"

    def inspect(self, seeds: Sequence[DecisionSeed]) -> RegistryPlan:
        selected = self._validated_seeds(seeds)
        state = self._load_state()
        product_id = selected[0].product_id
        product_name = selected[0].product_name
        entry = RootProductEntry(
            name=product_name,
            product_path=f"products/{product_id}/product.json",
            registry_path=f"products/{product_id}/registry.json",
        )
        current_entry = state.root.products.get(product_id)
        if current_entry is not None and current_entry != entry:
            raise RegistryInvalid("Product Registry identity conflicts")
        current_metadata = state.products.get(product_id)
        if current_metadata is not None and current_metadata.name != product_name:
            raise RegistryInvalid("Product metadata identity conflicts")
        metadata = current_metadata or ProductMetadata(product_id, product_name)
        current_registry = state.registries.get(product_id)
        next_registry = current_registry or ProductRegistry(product_id, {})

        decision_paths: list[str] = []
        for seed in selected:
            if seed.decision_id in next_registry.decisions:
                raise DecisionUpdateNotSupported(
                    f"Decision {seed.decision_id!r} already has a published head"
                )
            head_path = f"decisions/{seed.decision_id}/r0001.json"
            next_registry = next_registry.with_head(
                seed.decision_id,
                DecisionHead(1, "active", head_path),
            )
            decision_paths.append(
                f"decision-registry/products/{product_id}/{head_path}"
            )

        next_root = state.root.with_product(product_id, entry)
        product_path = f"decision-registry/{entry.product_path}"
        product_registry_path = f"decision-registry/{entry.registry_path}"
        resulting_documents = {
            ROOT_PATH: canonical_json_bytes(next_root.to_dict()),
            product_path: canonical_json_bytes(metadata.to_dict()),
            product_registry_path: canonical_json_bytes(next_registry.to_dict()),
        }
        relevant_paths = tuple(
            sorted((*resulting_documents.keys(), *decision_paths))
        )
        base_digests = {
            path: self._path_digest(path) for path in relevant_paths
        }
        changed_paths = set(decision_paths)
        for path, content in resulting_documents.items():
            if self._current_bytes(path) != content:
                changed_paths.add(path)

        return RegistryPlan(
            product_id=product_id,
            product_name=product_name,
            seeds=selected,
            decision_ids=tuple(seed.decision_id for seed in selected),
            decision_paths=tuple(decision_paths),
            changed_paths=tuple(sorted(changed_paths)),
            base_registry_digests=dict(sorted(base_digests.items())),
            current_root=state.root,
            next_root=next_root,
            product_metadata=metadata,
            current_product_registry=current_registry,
            next_product_registry=next_registry,
        )

    def render(self, plan: RegistryPlan, preview_id: str) -> RegistryDraft:
        if not isinstance(plan, RegistryPlan):
            raise TypeError("Registry render requires a RegistryPlan")
        if not isinstance(preview_id, str) or _PREVIEW_ID.fullmatch(preview_id) is None:
            raise RegistryInvalid("Publication preview id is invalid")
        product_base = f"decision-registry/products/{plan.product_id}"
        display: dict[str, bytes] = {
            ROOT_PATH: canonical_json_bytes(plan.next_root.to_dict()),
            f"{product_base}/product.json": canonical_json_bytes(
                plan.product_metadata.to_dict()
            ),
            f"{product_base}/registry.json": canonical_json_bytes(
                plan.next_product_registry.to_dict()
            ),
        }
        for seed, path in zip(plan.seeds, plan.decision_paths, strict=True):
            display[path] = canonical_json_bytes(
                DecisionRevision.from_seed(seed, preview_id).to_dict()
            )
        try:
            changed = {path: display[path] for path in plan.changed_paths}
        except KeyError:
            raise RegistryInvalid("Registry plan changed paths are invalid") from None
        return RegistryDraft(
            display_documents=dict(sorted(display.items())),
            changed_files=dict(sorted(changed.items())),
        )

    def assert_base(self, plan: RegistryPlan) -> None:
        if not isinstance(plan, RegistryPlan):
            raise TypeError("Registry base check requires a RegistryPlan")
        for path, expected in plan.base_registry_digests.items():
            try:
                actual = self._path_digest(path)
            except RegistryInvalid:
                raise RegistryStale("Registry base changed after preview") from None
            if actual != expected:
                raise RegistryStale("Registry base changed after preview")

    def write_exact(self, changed_files: Mapping[str, bytes]) -> None:
        if not isinstance(changed_files, Mapping) or not changed_files:
            raise RegistryConflict("Registry write set must not be empty")
        validated: list[tuple[Path, bytes]] = []
        for relative_path, content in changed_files.items():
            if not isinstance(relative_path, str) or not self._owned_formal_path(
                relative_path
            ):
                raise RegistryConflict("Registry write path is not owned")
            if not isinstance(content, bytes):
                raise RegistryConflict("Registry write content must be exact bytes")
            self._validate_formal_content(relative_path, content)
            path = self._safe_repo_path(relative_path, RegistryConflict)
            self._assert_write_components(path)
            validated.append((path, content))
        for path, content in validated:
            atomic_write_bytes(path, content)

    def _validated_seeds(
        self,
        seeds: Sequence[DecisionSeed],
    ) -> tuple[DecisionSeed, ...]:
        if isinstance(seeds, (str, bytes)) or not isinstance(seeds, Sequence):
            raise RegistryInvalid("Decision seeds must be a sequence")
        if not 1 <= len(seeds) <= 20:
            raise RegistryInvalid("Registry plan must contain 1 to 20 Decisions")
        if any(not isinstance(seed, DecisionSeed) for seed in seeds):
            raise RegistryInvalid("Registry plan contains an invalid Decision seed")
        selected = tuple(seeds)
        product_pairs = {(seed.product_id, seed.product_name) for seed in selected}
        if len(product_pairs) != 1:
            raise RegistryInvalid("One Registry plan must contain one product")
        decision_ids = [seed.decision_id for seed in selected]
        if len(decision_ids) != len(set(decision_ids)):
            raise RegistryInvalid("Registry plan contains a duplicate Decision")
        return selected

    def _load_state(self) -> _RegistryState:
        root_value = self._read_canonical_json(ROOT_PATH)
        try:
            root = RootRegistry.from_dict(root_value)
        except (TypeError, ValueError):
            raise RegistryInvalid("Root Registry is invalid") from None
        self._validate_root_children()
        products_directory = self.registry_root / "products"
        actual_product_ids = self._directory_names(products_directory)
        if actual_product_ids != set(root.products):
            raise RegistryInvalid("Registry product directories do not match the index")

        products: dict[str, ProductMetadata] = {}
        registries: dict[str, ProductRegistry] = {}
        for product_id, entry in root.products.items():
            product_directory = products_directory / product_id
            children = self._children(product_directory)
            if not {"product.json", "registry.json"}.issubset(children):
                raise RegistryInvalid("Product Registry files are missing")
            if set(children) - {"product.json", "registry.json", "decisions"}:
                raise RegistryInvalid("Product directory contains an unowned path")
            metadata_value = self._read_canonical_json(
                f"decision-registry/{entry.product_path}"
            )
            registry_value = self._read_canonical_json(
                f"decision-registry/{entry.registry_path}"
            )
            try:
                metadata = ProductMetadata.from_dict(metadata_value)
                product_registry = ProductRegistry.from_dict(registry_value)
            except (TypeError, ValueError):
                raise RegistryInvalid("Product Registry document is invalid") from None
            if (
                metadata.product_id != product_id
                or metadata.name != entry.name
                or product_registry.product_id != product_id
            ):
                raise RegistryInvalid("Product Registry ownership mismatch")
            self._validate_decisions(product_directory, metadata, product_registry)
            products[product_id] = metadata
            registries[product_id] = product_registry
        return _RegistryState(root, products, registries)

    def _validate_root_children(self) -> None:
        if not self.registry_root.exists() or not self.registry_root.is_dir():
            raise RegistryInvalid("Registry directory is unavailable")
        children = self._children(self.registry_root)
        if set(children) - {"README.md", "registry.json", "products"}:
            raise RegistryInvalid("Registry root contains an unowned path")
        if "registry.json" not in children:
            raise RegistryInvalid("Root Registry is unavailable")
        if children["registry.json"].is_dir():
            raise RegistryInvalid("Root Registry is not a file")
        if "products" in children and not children["products"].is_dir():
            raise RegistryInvalid("Registry products path is not a directory")

    def _validate_decisions(
        self,
        product_directory: Path,
        metadata: ProductMetadata,
        product_registry: ProductRegistry,
    ) -> None:
        decisions_directory = product_directory / "decisions"
        actual_decision_ids = self._directory_names(decisions_directory)
        if actual_decision_ids != set(product_registry.decisions):
            raise RegistryInvalid("Decision directories do not match the product index")
        for decision_id, head in product_registry.decisions.items():
            decision_directory = decisions_directory / decision_id
            children = self._children(decision_directory)
            expected_name = f"r{head.head_revision:04d}.json"
            if set(children) != {expected_name} or children[expected_name].is_dir():
                raise RegistryInvalid("Decision revision files are invalid")
            relative = (
                f"decision-registry/products/{metadata.product_id}/"
                f"decisions/{decision_id}/{expected_name}"
            )
            value = self._read_canonical_json(relative)
            try:
                revision = DecisionRevision.from_dict(value)
            except (TypeError, ValueError):
                raise RegistryInvalid("Decision revision is invalid") from None
            if (
                revision.decision_id != decision_id
                or revision.product_id != metadata.product_id
                or revision.product_name != metadata.name
                or revision.revision != head.head_revision
                or revision.lifecycle != head.lifecycle
            ):
                raise RegistryInvalid("Decision revision ownership mismatch")

    def _read_canonical_json(self, relative_path: str) -> Mapping[str, object]:
        path = self._safe_repo_path(relative_path, RegistryInvalid)
        if not path.exists() or not path.is_file() or path.is_symlink():
            raise RegistryInvalid("Registry document is unavailable")
        try:
            raw = path.read_bytes()
            value = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda _: self._reject_json_constant(),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise RegistryInvalid("Registry document is invalid JSON") from None
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise RegistryInvalid("Registry document is not canonical JSON")
        return value

    @staticmethod
    def _reject_json_constant() -> None:
        raise ValueError("Invalid JSON constant")

    def _safe_repo_path(
        self,
        relative_path: str,
        error_type: type[RegistryError],
    ) -> Path:
        pure = PurePosixPath(relative_path)
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.parts[0] != "decision-registry"
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            raise error_type("Registry path is invalid")
        path = self.repository_root.joinpath(*pure.parts)
        self._validate_path_components(path, error_type)
        return path

    def _validate_path_components(
        self,
        path: Path,
        error_type: type[RegistryError],
    ) -> None:
        current = self.registry_root
        if current.is_symlink():
            raise error_type("Registry path contains a symlink")
        try:
            relative = path.relative_to(self.registry_root)
        except ValueError:
            raise error_type("Registry path escapes its root") from None
        for part in relative.parts:
            current = current / part
            if os.path.lexists(current) and current.is_symlink():
                raise error_type("Registry path contains a symlink")

    def _path_digest(self, relative_path: str) -> str:
        path = self._safe_repo_path(relative_path, RegistryInvalid)
        if not os.path.lexists(path):
            return "missing"
        if path.is_symlink() or not path.is_file():
            raise RegistryInvalid("Registry digest path is invalid")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _current_bytes(self, relative_path: str) -> bytes | None:
        path = self._safe_repo_path(relative_path, RegistryInvalid)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RegistryInvalid("Registry document path is invalid")
        return path.read_bytes()

    def _children(self, directory: Path) -> dict[str, Path]:
        if directory.is_symlink() or not directory.exists() or not directory.is_dir():
            raise RegistryInvalid("Registry directory is invalid")
        children: dict[str, Path] = {}
        for child in directory.iterdir():
            if child.is_symlink():
                raise RegistryInvalid("Registry contains a symlink")
            children[child.name] = child
        return children

    def _directory_names(self, directory: Path) -> set[str]:
        if not directory.exists():
            return set()
        children = self._children(directory)
        if any(not child.is_dir() for child in children.values()):
            raise RegistryInvalid("Registry index directory contains a file")
        return set(children)

    def _assert_write_components(self, path: Path) -> None:
        self._validate_path_components(path, RegistryConflict)
        current = self.registry_root
        for part in path.relative_to(self.registry_root).parts[:-1]:
            current = current / part
            if os.path.lexists(current) and not current.is_dir():
                raise RegistryConflict("Registry parent path is not a directory")
        if os.path.lexists(path) and not path.is_file():
            raise RegistryConflict("Registry target path is not a file")

    def _validate_formal_content(self, path: str, content: bytes) -> None:
        try:
            value = json.loads(
                content.decode("utf-8"),
                parse_constant=lambda _: self._reject_json_constant(),
            )
            if not isinstance(value, dict) or canonical_json_bytes(value) != content:
                raise ValueError("Formal document is not canonical")
            if path == ROOT_PATH:
                RootRegistry.from_dict(value)
                return
            product_match = _PRODUCT_PATH.fullmatch(path)
            if product_match is not None:
                metadata = ProductMetadata.from_dict(value)
                if metadata.product_id != product_match.group(1):
                    raise ValueError("Product metadata path mismatch")
                return
            registry_match = _PRODUCT_REGISTRY_PATH.fullmatch(path)
            if registry_match is not None:
                product_registry = ProductRegistry.from_dict(value)
                if product_registry.product_id != registry_match.group(1):
                    raise ValueError("Product Registry path mismatch")
                return
            decision_match = _DECISION_PATH.fullmatch(path)
            if decision_match is not None:
                decision = DecisionRevision.from_dict(value)
                if (
                    decision.product_id != decision_match.group(1)
                    or decision.decision_id != decision_match.group(2)
                ):
                    raise ValueError("Decision revision path mismatch")
                return
            raise ValueError("Formal path is not owned")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise RegistryConflict("Registry write content is invalid") from None

    @staticmethod
    def _owned_formal_path(path: str) -> bool:
        if path == ROOT_PATH:
            return True
        return any(
            pattern.fullmatch(path) is not None
            for pattern in (
                _PRODUCT_PATH,
                _PRODUCT_REGISTRY_PATH,
                _DECISION_PATH,
            )
        )
