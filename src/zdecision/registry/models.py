"""Strict formal values owned by the Decision Registry."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from zdecision.capture.models import CandidateContent, SourceCheckpoint
from zdecision.capture.reviews import ApprovalRef
from zdecision.ids import (
    canonical_product_name,
    decision_id as derive_decision_id,
    product_id as derive_product_id,
)


ROOT_FORMAT = "zdecision-registry/v1"
PRODUCT_FORMAT = "zdecision-product/v1"
PRODUCT_REGISTRY_FORMAT = "zdecision-product-registry/v1"
DECISION_FORMAT = "zdecision-decision/v1"
SCHEMA_VERSION = 1

_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_DECISION_ID = re.compile(r"^dec_[0-9a-f]{32}$")
_CANDIDATE_ID = re.compile(
    r"^cand_[0-9a-f]{32}_(?:0[1-9]|1[0-9]|20)$"
)
_PREVIEW_ID = re.compile(r"^pub_[0-9a-f]{32}$")


def _require_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    object_name: str,
) -> None:
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise ValueError(f"{object_name} has invalid fields")


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return tuple(_text(member, field_name) for member in value)


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def _product_identity(product_id: str, name: str) -> None:
    if not isinstance(product_id, str) or _PRODUCT_ID.fullmatch(product_id) is None:
        raise ValueError("product_id is invalid")
    canonical_name = canonical_product_name(name)
    if canonical_name != name or derive_product_id(name) != product_id:
        raise ValueError("Product identity does not match its canonical name")


@dataclass(frozen=True)
class RootProductEntry:
    name: str
    product_path: str
    registry_path: str

    def __post_init__(self) -> None:
        _text(self.name, "Product name")
        _text(self.product_path, "Product metadata path")
        _text(self.registry_path, "Product Registry path")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "product_path": self.product_path,
            "registry_path": self.registry_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RootProductEntry:
        _require_fields(
            value,
            frozenset(("name", "product_path", "registry_path")),
            "RootProductEntry",
        )
        return cls(
            name=_text(value["name"], "Product name"),
            product_path=_text(value["product_path"], "Product metadata path"),
            registry_path=_text(value["registry_path"], "Product Registry path"),
        )


@dataclass(frozen=True)
class RootRegistry:
    products: Mapping[str, RootProductEntry]

    def __post_init__(self) -> None:
        if not isinstance(self.products, Mapping):
            raise ValueError("Root Registry products must be an object")
        normalized: dict[str, RootProductEntry] = {}
        for product_id, entry in self.products.items():
            if not isinstance(product_id, str) or not isinstance(
                entry, RootProductEntry
            ):
                raise ValueError("Root Registry product entry is invalid")
            _product_identity(product_id, entry.name)
            if entry.product_path != f"products/{product_id}/product.json":
                raise ValueError("Root product metadata path is invalid")
            if entry.registry_path != f"products/{product_id}/registry.json":
                raise ValueError("Root product Registry path is invalid")
            normalized[product_id] = entry
        object.__setattr__(
            self,
            "products",
            MappingProxyType(dict(sorted(normalized.items()))),
        )

    @classmethod
    def empty(cls) -> RootRegistry:
        return cls(products={})

    def with_product(
        self,
        product_id: str,
        entry: RootProductEntry,
    ) -> RootRegistry:
        products = dict(self.products)
        existing = products.get(product_id)
        if existing is not None and existing != entry:
            raise ValueError("Root Registry product identity conflicts")
        products[product_id] = entry
        return RootRegistry(products=products)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": ROOT_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "products": {
                product_id: entry.to_dict()
                for product_id, entry in self.products.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RootRegistry:
        _require_fields(
            value,
            frozenset(("format", "schema_version", "products")),
            "RootRegistry",
        )
        if value["format"] != ROOT_FORMAT or value["schema_version"] != 1:
            raise ValueError("Root Registry format is invalid")
        products_value = _mapping(value["products"], "Root Registry products")
        products: dict[str, RootProductEntry] = {}
        for product_id, entry in products_value.items():
            if not isinstance(product_id, str) or not isinstance(entry, Mapping):
                raise ValueError("Root Registry product entry is invalid")
            products[product_id] = RootProductEntry.from_dict(entry)
        return cls(products=products)


@dataclass(frozen=True)
class ProductMetadata:
    product_id: str
    name: str

    def __post_init__(self) -> None:
        _product_identity(self.product_id, self.name)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": PRODUCT_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "product_id": self.product_id,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProductMetadata:
        _require_fields(
            value,
            frozenset(("format", "schema_version", "product_id", "name")),
            "ProductMetadata",
        )
        if value["format"] != PRODUCT_FORMAT or value["schema_version"] != 1:
            raise ValueError("Product metadata format is invalid")
        return cls(
            product_id=_text(value["product_id"], "product_id"),
            name=_text(value["name"], "Product name"),
        )


@dataclass(frozen=True)
class DecisionHead:
    head_revision: int
    lifecycle: Literal["active"]
    head_path: str

    def __post_init__(self) -> None:
        if self.head_revision != 1 or isinstance(self.head_revision, bool):
            raise ValueError("Initial Decision head revision must be 1")
        if self.lifecycle != "active":
            raise ValueError("Initial Decision head lifecycle must be active")
        _text(self.head_path, "Decision head path")

    def to_dict(self) -> dict[str, object]:
        return {
            "head_revision": self.head_revision,
            "lifecycle": self.lifecycle,
            "head_path": self.head_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DecisionHead:
        _require_fields(
            value,
            frozenset(("head_revision", "lifecycle", "head_path")),
            "DecisionHead",
        )
        return cls(
            head_revision=value["head_revision"],
            lifecycle=value["lifecycle"],
            head_path=_text(value["head_path"], "Decision head path"),
        )


@dataclass(frozen=True)
class ProductRegistry:
    product_id: str
    decisions: Mapping[str, DecisionHead]

    def __post_init__(self) -> None:
        if not isinstance(self.product_id, str) or _PRODUCT_ID.fullmatch(
            self.product_id
        ) is None:
            raise ValueError("Product Registry product_id is invalid")
        if not isinstance(self.decisions, Mapping):
            raise ValueError("Product Registry decisions must be an object")
        normalized: dict[str, DecisionHead] = {}
        for decision_id, head in self.decisions.items():
            if (
                not isinstance(decision_id, str)
                or _DECISION_ID.fullmatch(decision_id) is None
                or not isinstance(head, DecisionHead)
            ):
                raise ValueError("Product Registry Decision entry is invalid")
            expected = f"decisions/{decision_id}/r{head.head_revision:04d}.json"
            if head.head_path != expected:
                raise ValueError("Decision head path does not match its identity")
            normalized[decision_id] = head
        object.__setattr__(
            self,
            "decisions",
            MappingProxyType(dict(sorted(normalized.items()))),
        )

    def with_head(self, decision_id: str, head: DecisionHead) -> ProductRegistry:
        decisions = dict(self.decisions)
        if decision_id in decisions:
            raise ValueError("Decision head already exists")
        decisions[decision_id] = head
        return ProductRegistry(product_id=self.product_id, decisions=decisions)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": PRODUCT_REGISTRY_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "product_id": self.product_id,
            "decisions": {
                decision_id: head.to_dict()
                for decision_id, head in self.decisions.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProductRegistry:
        _require_fields(
            value,
            frozenset(("format", "schema_version", "product_id", "decisions")),
            "ProductRegistry",
        )
        if (
            value["format"] != PRODUCT_REGISTRY_FORMAT
            or value["schema_version"] != 1
        ):
            raise ValueError("Product Registry format is invalid")
        decisions_value = _mapping(value["decisions"], "Product decisions")
        decisions: dict[str, DecisionHead] = {}
        for decision_id, head in decisions_value.items():
            if not isinstance(decision_id, str) or not isinstance(head, Mapping):
                raise ValueError("Product Registry Decision entry is invalid")
            decisions[decision_id] = DecisionHead.from_dict(head)
        return cls(
            product_id=_text(value["product_id"], "product_id"),
            decisions=decisions,
        )


@dataclass(frozen=True)
class DecisionSeed:
    candidate_id: str
    decision_id: str
    product_id: str
    product_name: str
    content: CandidateContent
    source: SourceCheckpoint
    review_approval: ApprovalRef

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or _CANDIDATE_ID.fullmatch(
            self.candidate_id
        ) is None:
            raise ValueError("Decision seed Candidate id is invalid")
        _product_identity(self.product_id, self.product_name)
        if self.decision_id != derive_decision_id(
            self.candidate_id, self.product_id
        ):
            raise ValueError("Decision seed identity mismatch")
        if not isinstance(self.content, CandidateContent):
            raise ValueError("Decision seed content is invalid")
        CandidateContent.from_dict(self.content.to_dict())
        if self.content.product != self.product_name:
            raise ValueError("Decision seed content product is invalid")
        if not isinstance(self.source, SourceCheckpoint):
            raise ValueError("Decision seed source is invalid")
        if not isinstance(self.review_approval, ApprovalRef):
            raise ValueError("Decision seed Review approval is invalid")


@dataclass(frozen=True)
class DecisionRevision:
    decision_id: str
    product_id: str
    product_name: str
    revision: Literal[1]
    lifecycle: Literal["active"]
    claim: str
    future_action: str
    scope_summary: str
    repositories: tuple[str, ...]
    paths: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    supersedes: tuple[object, ...]
    variant_of: tuple[object, ...]
    source: SourceCheckpoint
    review_approval: ApprovalRef
    publication_preview_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, str) or _DECISION_ID.fullmatch(
            self.decision_id
        ) is None:
            raise ValueError("Decision id is invalid")
        _product_identity(self.product_id, self.product_name)
        if self.revision != 1 or isinstance(self.revision, bool):
            raise ValueError("Initial Decision revision must be 1")
        if self.lifecycle != "active":
            raise ValueError("Initial Decision lifecycle must be active")
        _text(self.claim, "Decision claim")
        _text(self.future_action, "Decision future action")
        _text(self.scope_summary, "Decision scope summary")
        for field_name, members in (
            ("repositories", self.repositories),
            ("paths", self.paths),
            ("invalidation_conditions", self.invalidation_conditions),
        ):
            if not isinstance(members, tuple):
                raise ValueError(f"Decision {field_name} must be a tuple")
            for member in members:
                _text(member, f"Decision {field_name}")
        if self.supersedes != () or self.variant_of != ():
            raise ValueError("Initial Decision relations must be empty")
        if not isinstance(self.source, SourceCheckpoint):
            raise ValueError("Decision source is invalid")
        if not isinstance(self.review_approval, ApprovalRef):
            raise ValueError("Decision Review approval is invalid")
        if not isinstance(self.publication_preview_id, str) or _PREVIEW_ID.fullmatch(
            self.publication_preview_id
        ) is None:
            raise ValueError("Decision publication preview id is invalid")

    @classmethod
    def from_seed(
        cls,
        seed: DecisionSeed,
        preview_id: str,
    ) -> DecisionRevision:
        return cls(
            decision_id=seed.decision_id,
            product_id=seed.product_id,
            product_name=seed.product_name,
            revision=1,
            lifecycle="active",
            claim=seed.content.claim,
            future_action=seed.content.future_action,
            scope_summary=seed.content.scope_summary,
            repositories=seed.content.repositories,
            paths=seed.content.paths,
            invalidation_conditions=seed.content.invalidation_conditions,
            supersedes=(),
            variant_of=(),
            source=seed.source,
            review_approval=seed.review_approval,
            publication_preview_id=preview_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "format": DECISION_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "revision": self.revision,
            "lifecycle": self.lifecycle,
            "claim": self.claim,
            "future_action": self.future_action,
            "scope": {
                "summary": self.scope_summary,
                "repositories": list(self.repositories),
                "paths": list(self.paths),
            },
            "invalidation_conditions": list(self.invalidation_conditions),
            "supersedes": [],
            "variant_of": [],
            "source": self.source.to_dict(),
            "review_approval": self.review_approval.to_dict(),
            "publication_preview_id": self.publication_preview_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DecisionRevision:
        _require_fields(
            value,
            frozenset(
                (
                    "format",
                    "schema_version",
                    "decision_id",
                    "product_id",
                    "product_name",
                    "revision",
                    "lifecycle",
                    "claim",
                    "future_action",
                    "scope",
                    "invalidation_conditions",
                    "supersedes",
                    "variant_of",
                    "source",
                    "review_approval",
                    "publication_preview_id",
                )
            ),
            "DecisionRevision",
        )
        if value["format"] != DECISION_FORMAT or value["schema_version"] != 1:
            raise ValueError("Decision format is invalid")
        scope = _mapping(value["scope"], "Decision scope")
        _require_fields(
            scope,
            frozenset(("summary", "repositories", "paths")),
            "Decision scope",
        )
        source = _mapping(value["source"], "Decision source")
        approval = _mapping(value["review_approval"], "Decision Review approval")
        return cls(
            decision_id=_text(value["decision_id"], "decision_id"),
            product_id=_text(value["product_id"], "product_id"),
            product_name=_text(value["product_name"], "product_name"),
            revision=value["revision"],
            lifecycle=value["lifecycle"],
            claim=_text(value["claim"], "Decision claim"),
            future_action=_text(value["future_action"], "Decision future action"),
            scope_summary=_text(scope["summary"], "Decision scope summary"),
            repositories=_strings(scope["repositories"], "Decision repositories"),
            paths=_strings(scope["paths"], "Decision paths"),
            invalidation_conditions=_strings(
                value["invalidation_conditions"],
                "Decision invalidation conditions",
            ),
            supersedes=_strings(value["supersedes"], "Decision supersedes"),
            variant_of=_strings(value["variant_of"], "Decision variant_of"),
            source=SourceCheckpoint.from_dict(source),
            review_approval=ApprovalRef.from_dict(approval),
            publication_preview_id=_text(
                value["publication_preview_id"],
                "publication_preview_id",
            ),
        )
