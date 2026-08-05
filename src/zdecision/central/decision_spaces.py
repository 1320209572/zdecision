"""Trusted leaf Decision-space catalog and repository routing values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from zdecision.ids import canonical_product_name


DecisionSpaceKind = Literal["product", "shared_unit"]

_CATALOG_GROUP_ID = re.compile(r"^dsg_[0-9a-f]{32}$")
_DECISION_SPACE_ID = re.compile(r"^dsp_[0-9a-f]{32}$")
_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_ROUTE_ID = re.compile(r"^drr_[0-9a-f]{32}$")


def _mapping(value: object, fields: frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise ValueError(f"{name} fields are invalid")
    return value


def _identifier(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _name(value: object, field_name: str) -> str:
    try:
        result = canonical_product_name(cast(str, value))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} is invalid") from None
    if result != value:
        raise ValueError(f"{field_name} is invalid")
    return result


def _prefix(value: object, field_name: str, *, root_allowed: bool = False) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError(f"{field_name} is invalid")
    if value == ".":
        if root_allowed:
            return value
        raise ValueError(f"{field_name} is invalid")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{field_name} is invalid")
    return value


def _prefixes(value: object, field_name: str, *, root_allowed: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field_name} is invalid")
    result = tuple(_prefix(item, field_name, root_allowed=root_allowed) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} contains duplicates")
    return result


def _breadcrumbs(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} is invalid")
    return tuple(_name(item, field_name) for item in value)


@dataclass(frozen=True)
class CatalogGroup:
    catalog_group_id: str
    parent_group_id: str | None
    display_name: str
    breadcrumb: tuple[str, ...]
    source_prefix: str | None
    sort_order: int

    def __post_init__(self) -> None:
        _identifier(self.catalog_group_id, _CATALOG_GROUP_ID, "catalog_group_id")
        if self.parent_group_id is not None:
            _identifier(self.parent_group_id, _CATALOG_GROUP_ID, "parent_group_id")
        _name(self.display_name, "display_name")
        if not isinstance(self.breadcrumb, tuple) or not self.breadcrumb:
            raise ValueError("breadcrumb is invalid")
        if _breadcrumbs(self.breadcrumb, "breadcrumb") != self.breadcrumb:
            raise ValueError("breadcrumb is invalid")
        if self.breadcrumb[-1] != self.display_name:
            raise ValueError("breadcrumb is invalid")
        if self.source_prefix is not None:
            _prefix(self.source_prefix, "source_prefix")
        if not isinstance(self.sort_order, int) or isinstance(self.sort_order, bool):
            raise ValueError("sort_order is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog_group_id": self.catalog_group_id,
            "parent_group_id": self.parent_group_id,
            "display_name": self.display_name,
            "breadcrumb": list(self.breadcrumb),
            "source_prefix": self.source_prefix,
            "sort_order": self.sort_order,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CatalogGroup":
        item = _mapping(value, frozenset(("catalog_group_id", "parent_group_id", "display_name", "breadcrumb", "source_prefix", "sort_order")), "CatalogGroup")
        return cls(item["catalog_group_id"], item["parent_group_id"], item["display_name"], _breadcrumbs(item["breadcrumb"], "breadcrumb"), item["source_prefix"], item["sort_order"])


@dataclass(frozen=True)
class LeafDecisionSpace:
    decision_space_id: str
    kind: DecisionSpaceKind
    display_name: str
    compatibility_product_id: str
    compatibility_product_name: str
    catalog_group_id: str | None
    catalog_breadcrumb: tuple[str, ...]
    source_root: str
    package_name: str | None
    asset_type: str | None
    enabled: bool

    def __post_init__(self) -> None:
        _identifier(self.decision_space_id, _DECISION_SPACE_ID, "decision_space_id")
        if self.kind not in ("product", "shared_unit"):
            raise ValueError("kind is invalid")
        _name(self.display_name, "display_name")
        _identifier(self.compatibility_product_id, _PRODUCT_ID, "compatibility_product_id")
        _name(self.compatibility_product_name, "compatibility_product_name")
        if self.catalog_group_id is not None:
            _identifier(self.catalog_group_id, _CATALOG_GROUP_ID, "catalog_group_id")
        if not isinstance(self.catalog_breadcrumb, tuple):
            raise ValueError("catalog_breadcrumb is invalid")
        if _breadcrumbs(self.catalog_breadcrumb, "catalog_breadcrumb") != self.catalog_breadcrumb:
            raise ValueError("catalog_breadcrumb is invalid")
        _prefix(
            self.source_root,
            "source_root",
            root_allowed=self.kind == "product",
        )
        if self.kind == "shared_unit" and self.source_root in (
            "packages/products/shared", "packages/shared",
        ):
            raise ValueError("shared_leaf_source_root_is_aggregate")
        for field_name in ("package_name", "asset_type"):
            field = getattr(self, field_name)
            if field is not None and (not isinstance(field, str) or not field.strip()):
                raise ValueError(f"{field_name} is invalid")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"decision_space_id": self.decision_space_id, "kind": self.kind, "display_name": self.display_name, "compatibility_product_id": self.compatibility_product_id, "compatibility_product_name": self.compatibility_product_name, "catalog_group_id": self.catalog_group_id, "catalog_breadcrumb": list(self.catalog_breadcrumb), "source_root": self.source_root, "package_name": self.package_name, "asset_type": self.asset_type, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, value: object) -> "LeafDecisionSpace":
        item = _mapping(value, frozenset(("decision_space_id", "kind", "display_name", "compatibility_product_id", "compatibility_product_name", "catalog_group_id", "catalog_breadcrumb", "source_root", "package_name", "asset_type", "enabled")), "LeafDecisionSpace")
        return cls(item["decision_space_id"], item["kind"], item["display_name"], item["compatibility_product_id"], item["compatibility_product_name"], item["catalog_group_id"], _breadcrumbs(item["catalog_breadcrumb"], "catalog_breadcrumb"), item["source_root"], item["package_name"], item["asset_type"], item["enabled"])


@dataclass(frozen=True)
class RepositoryDecisionRoute:
    route_id: str
    repository_id: str
    decision_space_id: str
    path_prefixes: tuple[str, ...]
    excluded_prefixes: tuple[str, ...]
    enabled: bool
    configuration_version: int

    def __post_init__(self) -> None:
        _identifier(self.route_id, _ROUTE_ID, "route_id")
        _identifier(self.repository_id, _REPOSITORY_ID, "repository_id")
        if not isinstance(self.decision_space_id, str) or (
            _DECISION_SPACE_ID.fullmatch(self.decision_space_id) is None
            and _CATALOG_GROUP_ID.fullmatch(self.decision_space_id) is None
        ):
            raise ValueError("decision_space_id is invalid")
        if not isinstance(self.path_prefixes, tuple):
            raise ValueError("path_prefixes is invalid")
        if _prefixes(self.path_prefixes, "path_prefixes", root_allowed=True) != self.path_prefixes:
            raise ValueError("path_prefixes is invalid")
        if not isinstance(self.excluded_prefixes, tuple):
            raise ValueError("excluded_prefixes is invalid")
        if self.excluded_prefixes and _prefixes(self.excluded_prefixes, "excluded_prefixes") != self.excluded_prefixes:
            raise ValueError("excluded_prefixes is invalid")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled is invalid")
        if not isinstance(self.configuration_version, int) or isinstance(self.configuration_version, bool) or self.configuration_version < 1:
            raise ValueError("configuration_version is invalid")

    def matches(self, path: str) -> bool:
        path = _prefix(path, "path")
        included = any(prefix == "." or path == prefix or path.startswith(prefix + "/") for prefix in self.path_prefixes)
        excluded = any(path == prefix or path.startswith(prefix + "/") for prefix in self.excluded_prefixes)
        return included and not excluded

    def to_dict(self) -> dict[str, object]:
        return {"route_id": self.route_id, "repository_id": self.repository_id, "decision_space_id": self.decision_space_id, "path_prefixes": list(self.path_prefixes), "excluded_prefixes": list(self.excluded_prefixes), "enabled": self.enabled, "configuration_version": self.configuration_version}

    @classmethod
    def from_dict(cls, value: object) -> "RepositoryDecisionRoute":
        item = _mapping(value, frozenset(("route_id", "repository_id", "decision_space_id", "path_prefixes", "excluded_prefixes", "enabled", "configuration_version")), "RepositoryDecisionRoute")
        return cls(item["route_id"], item["repository_id"], item["decision_space_id"], _prefixes(item["path_prefixes"], "path_prefixes", root_allowed=True), tuple() if item["excluded_prefixes"] == [] else _prefixes(item["excluded_prefixes"], "excluded_prefixes"), item["enabled"], item["configuration_version"])


@dataclass(frozen=True)
class EnabledRepository:
    repository_id: str
    enabled: bool

    def __post_init__(self) -> None:
        _identifier(self.repository_id, _REPOSITORY_ID, "repository_id")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"repository_id": self.repository_id, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, value: object) -> "EnabledRepository":
        item = _mapping(value, frozenset(("repository_id", "enabled")), "EnabledRepository")
        return cls(item["repository_id"], item["enabled"])


@dataclass(frozen=True)
class RepositoryCatalogView:
    repository_id: str
    enabled: bool
    spaces: tuple[LeafDecisionSpace, ...]
    routes: tuple[RepositoryDecisionRoute, ...]
    shared_tree: CatalogGroup | None

    def __post_init__(self) -> None:
        _identifier(self.repository_id, _REPOSITORY_ID, "repository_id")
        if not isinstance(self.enabled, bool) or not isinstance(self.spaces, tuple) or not isinstance(self.routes, tuple):
            raise ValueError("RepositoryCatalogView is invalid")
        if any(not isinstance(item, LeafDecisionSpace) for item in self.spaces) or any(not isinstance(item, RepositoryDecisionRoute) for item in self.routes):
            raise ValueError("RepositoryCatalogView is invalid")
        if self.shared_tree is not None and not isinstance(self.shared_tree, CatalogGroup):
            raise ValueError("RepositoryCatalogView is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"repository_id": self.repository_id, "enabled": self.enabled, "spaces": [item.to_dict() for item in self.spaces], "routes": [item.to_dict() for item in self.routes], "shared_tree": None if self.shared_tree is None else self.shared_tree.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> "RepositoryCatalogView":
        item = _mapping(value, frozenset(("repository_id", "enabled", "spaces", "routes", "shared_tree")), "RepositoryCatalogView")
        if not isinstance(item["spaces"], list) or not isinstance(item["routes"], list):
            raise ValueError("RepositoryCatalogView fields are invalid")
        return cls(item["repository_id"], item["enabled"], tuple(LeafDecisionSpace.from_dict(space) for space in item["spaces"]), tuple(RepositoryDecisionRoute.from_dict(route) for route in item["routes"]), None if item["shared_tree"] is None else CatalogGroup.from_dict(item["shared_tree"]))
