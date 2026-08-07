"""Strict transport values for on-demand Candidate Capture."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from zdecision.capture.models import CandidateContent
from zdecision.capture.provenance import CandidateProvenanceSummary
from zdecision.ids import candidate_revision_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.central.decision_spaces import (
    DecisionSpaceKind,
    EnabledRepository,
    RepositoryCatalogView,
    RepositoryDecisionRoute,
)


CaptureRequestState = Literal[
    "queued",
    "claimed",
    "running",
    "succeeded",
    "succeeded_no_candidates",
    "failed_retryable",
    "failed_terminal",
    "cancelled",
]
CaptureScope = Literal["current_session", "all_valid_sessions"]

CAPTURE_REQUEST_LEASE_SECONDS = 30
CAPTURE_REQUEST_RENEW_INTERVAL_SECONDS = 10.0

_REQUEST_STATES = frozenset(
    (
        "queued",
        "claimed",
        "running",
        "succeeded",
        "succeeded_no_candidates",
        "failed_retryable",
        "failed_terminal",
        "cancelled",
    )
)
_CAPTURE_SCOPES = frozenset(("current_session", "all_valid_sessions"))
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_SLICE_ID = re.compile(r"^csl_[0-9a-f]{32}$")
_ROUTE_ID = re.compile(r"^drr_[0-9a-f]{32}$")
_DECISION_SPACE_ID = re.compile(r"^dsp_[0-9a-f]{32}$")
_FAMILY_ID = re.compile(r"^cfm_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^crv_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LEASE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_MAX_ITEMS = 20
_MAX_ITEM_BYTES = 16 * 1024
_MAX_BATCH_BYTES = 1024 * 1024


def _mapping(
    value: object,
    expected: frozenset[str],
    name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise ValueError(f"{name} fields are invalid")
    return value


def _pattern(
    value: object,
    pattern: re.Pattern[str],
    field_name: str,
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _safe_identifier(value: object, field_name: str) -> str:
    return _pattern(value, _SAFE_IDENTIFIER, field_name)


def _nonempty(value: object, field_name: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _positive_integer(value: object, field_name: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"{field_name} is invalid") from None
    if parsed.utcoffset() is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _state(value: object) -> CaptureRequestState:
    if value not in _REQUEST_STATES:
        raise ValueError("Capture Request state is invalid")
    return cast(CaptureRequestState, value)


def _capture_scope(value: object) -> CaptureScope:
    if value not in _CAPTURE_SCOPES:
        raise ValueError("capture_scope is invalid")
    return cast(CaptureScope, value)


def _optional_nonnegative_integer(
    value: object, field_name: str
) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _content(value: object) -> CandidateContent:
    if isinstance(value, CandidateContent):
        return CandidateContent.from_dict(value.to_dict())
    if not isinstance(value, Mapping):
        raise ValueError("Candidate content is invalid")
    return CandidateContent.from_dict(value)


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class RepositoryView:
    repository_id: str
    product_id: str
    product_name: str
    enabled: bool

    def __post_init__(self) -> None:
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        _pattern(self.product_id, _PRODUCT_ID, "product_id")
        _nonempty(self.product_name, "product_name")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: object) -> RepositoryView:
        item = _mapping(
            value,
            frozenset(("repository_id", "product_id", "product_name", "enabled")),
            "RepositoryView",
        )
        return cls(
            repository_id=item["repository_id"],
            product_id=item["product_id"],
            product_name=item["product_name"],
            enabled=item["enabled"],
        )


@dataclass(frozen=True)
class CaptureRequestCreate:
    repository_id: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str

    def __post_init__(self) -> None:
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        _safe_identifier(self.template_id, "template_id")
        _capture_scope(self.capture_scope)
        _safe_identifier(self.client_action_id, "client_action_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "template_id": self.template_id,
            "capture_scope": self.capture_scope,
            "client_action_id": self.client_action_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> CaptureRequestCreate:
        item = _mapping(
            value,
            frozenset(
                (
                    "repository_id", "template_id", "capture_scope",
                    "client_action_id",
                )
            ),
            "CaptureRequestCreate",
        )
        return cls(
            repository_id=item["repository_id"],
            template_id=item["template_id"],
            capture_scope=item["capture_scope"],
            client_action_id=item["client_action_id"],
        )


@dataclass(frozen=True)
class CaptureGroupCreate:
    repository_id: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str

    def __post_init__(self) -> None:
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        _safe_identifier(self.template_id, "template_id")
        _capture_scope(self.capture_scope)
        _safe_identifier(self.client_action_id, "client_action_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "template_id": self.template_id,
            "capture_scope": self.capture_scope,
            "client_action_id": self.client_action_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CaptureGroupCreate":
        item = _mapping(
            value,
            frozenset(("repository_id", "template_id", "capture_scope", "client_action_id")),
            "CaptureGroupCreate",
        )
        return cls(item["repository_id"], item["template_id"], item["capture_scope"], item["client_action_id"])


@dataclass(frozen=True)
class CaptureGroupView:
    request_id: str
    repository_id: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str
    state: CaptureRequestState
    last_sequence: int

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        _safe_identifier(self.template_id, "template_id")
        _capture_scope(self.capture_scope)
        _safe_identifier(self.client_action_id, "client_action_id")
        _state(self.state)
        _positive_integer(self.last_sequence, "last_sequence")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "template_id": self.template_id,
            "capture_scope": self.capture_scope,
            "client_action_id": self.client_action_id,
            "state": self.state,
            "last_sequence": self.last_sequence,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CaptureGroupView":
        item = _mapping(value, frozenset(("request_id", "repository_id", "template_id", "capture_scope", "client_action_id", "state", "last_sequence")), "CaptureGroupView")
        return cls(item["request_id"], item["repository_id"], item["template_id"], item["capture_scope"], item["client_action_id"], item["state"], item["last_sequence"])


@dataclass(frozen=True)
class ClaimedCaptureGroup:
    request_id: str
    repository_id: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str
    route_snapshot: tuple[RepositoryDecisionRoute, ...]
    route_snapshot_digest: str
    lease_token: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        _safe_identifier(self.template_id, "template_id")
        _capture_scope(self.capture_scope)
        _safe_identifier(self.client_action_id, "client_action_id")
        if not isinstance(self.route_snapshot, tuple) or any(not isinstance(route, RepositoryDecisionRoute) for route in self.route_snapshot):
            raise ValueError("route_snapshot is invalid")
        _pattern(self.route_snapshot_digest, _DIGEST, "route_snapshot_digest")
        if self.route_snapshot_digest != _sha256({"routes": [route.to_dict() for route in self.route_snapshot]}):
            raise ValueError("route_snapshot_digest does not match route snapshot")
        _pattern(self.lease_token, _LEASE_TOKEN, "lease_token")
        _timestamp(self.lease_expires_at, "lease_expires_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "template_id": self.template_id,
            "capture_scope": self.capture_scope,
            "client_action_id": self.client_action_id,
            "route_snapshot": [route.to_dict() for route in self.route_snapshot],
            "route_snapshot_digest": self.route_snapshot_digest,
            "lease_token": self.lease_token,
            "lease_expires_at": self.lease_expires_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ClaimedCaptureGroup":
        item = _mapping(value, frozenset(("request_id", "repository_id", "template_id", "capture_scope", "client_action_id", "route_snapshot", "route_snapshot_digest", "lease_token", "lease_expires_at")), "ClaimedCaptureGroup")
        routes = item["route_snapshot"]
        if not isinstance(routes, list):
            raise ValueError("route_snapshot is invalid")
        return cls(item["request_id"], item["repository_id"], item["template_id"], item["capture_scope"], item["client_action_id"], tuple(RepositoryDecisionRoute.from_dict(route) for route in routes), item["route_snapshot_digest"], item["lease_token"], item["lease_expires_at"])


@dataclass(frozen=True)
class RouteSelection:
    route_id: str
    configuration_version: int
    matched_path_digest: str
    source_boundary_digest: str

    def __post_init__(self) -> None:
        _pattern(self.route_id, _ROUTE_ID, "route_id")
        _positive_integer(self.configuration_version, "configuration_version")
        _pattern(self.matched_path_digest, _DIGEST, "matched_path_digest")
        _pattern(self.source_boundary_digest, _DIGEST, "source_boundary_digest")

    def to_dict(self) -> dict[str, object]:
        return {"route_id": self.route_id, "configuration_version": self.configuration_version, "matched_path_digest": self.matched_path_digest, "source_boundary_digest": self.source_boundary_digest}

    @classmethod
    def from_dict(cls, value: object) -> "RouteSelection":
        item = _mapping(value, frozenset(("route_id", "configuration_version", "matched_path_digest", "source_boundary_digest")), "RouteSelection")
        return cls(item["route_id"], item["configuration_version"], item["matched_path_digest"], item["source_boundary_digest"])


@dataclass(frozen=True)
class CandidateOwnershipSnapshot:
    repository_id: str
    route_id: str
    route_configuration_version: int
    decision_space_id: str
    decision_space_kind: DecisionSpaceKind
    display_name: str
    catalog_breadcrumb: tuple[str, ...]
    source_root: str
    compatibility_product_id: str
    compatibility_product_name: str
    source_boundary_digest: str

    def __post_init__(self) -> None:
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        _pattern(self.route_id, _ROUTE_ID, "route_id")
        _positive_integer(self.route_configuration_version, "route_configuration_version")
        _pattern(self.decision_space_id, _DECISION_SPACE_ID, "decision_space_id")
        if self.decision_space_kind not in ("product", "shared_unit"):
            raise ValueError("decision_space_kind is invalid")
        _nonempty(self.display_name, "display_name")
        if not isinstance(self.catalog_breadcrumb, tuple) or any(not isinstance(item, str) or not item for item in self.catalog_breadcrumb):
            raise ValueError("catalog_breadcrumb is invalid")
        _nonempty(self.source_root, "source_root")
        _pattern(self.compatibility_product_id, _PRODUCT_ID, "compatibility_product_id")
        _nonempty(self.compatibility_product_name, "compatibility_product_name")
        _pattern(self.source_boundary_digest, _DIGEST, "source_boundary_digest")

    def to_dict(self) -> dict[str, object]:
        return {"repository_id": self.repository_id, "route_id": self.route_id, "route_configuration_version": self.route_configuration_version, "decision_space_id": self.decision_space_id, "decision_space_kind": self.decision_space_kind, "display_name": self.display_name, "catalog_breadcrumb": list(self.catalog_breadcrumb), "source_root": self.source_root, "compatibility_product_id": self.compatibility_product_id, "compatibility_product_name": self.compatibility_product_name, "source_boundary_digest": self.source_boundary_digest}

    @classmethod
    def from_dict(cls, value: object) -> "CandidateOwnershipSnapshot":
        item = _mapping(value, frozenset(("repository_id", "route_id", "route_configuration_version", "decision_space_id", "decision_space_kind", "display_name", "catalog_breadcrumb", "source_root", "compatibility_product_id", "compatibility_product_name", "source_boundary_digest")), "CandidateOwnershipSnapshot")
        breadcrumb = item["catalog_breadcrumb"]
        if not isinstance(breadcrumb, list):
            raise ValueError("catalog_breadcrumb is invalid")
        return cls(item["repository_id"], item["route_id"], item["route_configuration_version"], item["decision_space_id"], item["decision_space_kind"], item["display_name"], tuple(breadcrumb), item["source_root"], item["compatibility_product_id"], item["compatibility_product_name"], item["source_boundary_digest"])


@dataclass(frozen=True)
class CaptureSliceView:
    request_id: str
    slice_id: str
    slice_order: int
    ownership: CandidateOwnershipSnapshot
    state: str

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.slice_id, _SLICE_ID, "slice_id")
        if not isinstance(self.slice_order, int) or isinstance(self.slice_order, bool) or self.slice_order < 0:
            raise ValueError("slice_order is invalid")
        if not isinstance(self.ownership, CandidateOwnershipSnapshot):
            raise ValueError("ownership is invalid")
        if self.state not in ("planned", "accepted"):
            raise ValueError("slice state is invalid")

    def to_dict(self) -> dict[str, object]:
        return {"request_id": self.request_id, "slice_id": self.slice_id, "slice_order": self.slice_order, "ownership": self.ownership.to_dict(), "state": self.state}

    @classmethod
    def from_dict(cls, value: object) -> "CaptureSliceView":
        item = _mapping(value, frozenset(("request_id", "slice_id", "slice_order", "ownership", "state")), "CaptureSliceView")
        return cls(item["request_id"], item["slice_id"], item["slice_order"], CandidateOwnershipSnapshot.from_dict(item["ownership"]), item["state"])


@dataclass(frozen=True)
class CaptureRequestView:
    request_id: str
    repository_id: str
    product_id: str
    product_name: str
    template_id: str
    state: CaptureRequestState
    progress_code: str
    candidate_revision_count: int | None
    last_sequence: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        _pattern(self.product_id, _PRODUCT_ID, "product_id")
        _nonempty(self.product_name, "product_name")
        _safe_identifier(self.template_id, "template_id")
        _state(self.state)
        _safe_identifier(self.progress_code, "progress_code")
        _optional_nonnegative_integer(
            self.candidate_revision_count, "candidate_revision_count"
        )
        _positive_integer(self.last_sequence, "last_sequence")
        _timestamp(self.created_at, "created_at")
        _timestamp(self.updated_at, "updated_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "template_id": self.template_id,
            "state": self.state,
            "progress_code": self.progress_code,
            "candidate_revision_count": self.candidate_revision_count,
            "last_sequence": self.last_sequence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> CaptureRequestView:
        item = _mapping(
            value,
            frozenset(
                (
                    "request_id",
                    "repository_id",
                    "product_id",
                    "product_name",
                    "template_id",
                    "state",
                    "progress_code",
                    "candidate_revision_count",
                    "last_sequence",
                    "created_at",
                    "updated_at",
                )
            ),
            "CaptureRequestView",
        )
        return cls(
            request_id=item["request_id"],
            repository_id=item["repository_id"],
            product_id=item["product_id"],
            product_name=item["product_name"],
            template_id=item["template_id"],
            state=item["state"],
            progress_code=item["progress_code"],
            candidate_revision_count=item["candidate_revision_count"],
            last_sequence=item["last_sequence"],
            created_at=item["created_at"],
            updated_at=item["updated_at"],
        )


@dataclass(frozen=True)
class ClaimedCaptureRequest:
    request_id: str
    repository_id: str
    product_id: str
    product_name: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str
    lease_token: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        _pattern(self.product_id, _PRODUCT_ID, "product_id")
        _nonempty(self.product_name, "product_name")
        _safe_identifier(self.template_id, "template_id")
        _capture_scope(self.capture_scope)
        _safe_identifier(self.client_action_id, "client_action_id")
        _pattern(self.lease_token, _LEASE_TOKEN, "lease_token")
        _timestamp(self.lease_expires_at, "lease_expires_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "template_id": self.template_id,
            "capture_scope": self.capture_scope,
            "client_action_id": self.client_action_id,
            "lease_token": self.lease_token,
            "lease_expires_at": self.lease_expires_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> ClaimedCaptureRequest:
        item = _mapping(
            value,
            frozenset(
                (
                    "request_id",
                    "repository_id",
                    "product_id",
                    "product_name",
                    "template_id",
                    "capture_scope",
                    "client_action_id",
                    "lease_token",
                    "lease_expires_at",
                )
            ),
            "ClaimedCaptureRequest",
        )
        return cls(
            request_id=item["request_id"],
            repository_id=item["repository_id"],
            product_id=item["product_id"],
            product_name=item["product_name"],
            template_id=item["template_id"],
            capture_scope=item["capture_scope"],
            client_action_id=item["client_action_id"],
            lease_token=item["lease_token"],
            lease_expires_at=item["lease_expires_at"],
        )


@dataclass(frozen=True)
class ProgressEvent:
    request_id: str
    sequence: int
    state: CaptureRequestState
    code: str
    occurred_at: str

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _positive_integer(self.sequence, "sequence")
        _state(self.state)
        _safe_identifier(self.code, "code")
        _timestamp(self.occurred_at, "occurred_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "state": self.state,
            "code": self.code,
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> ProgressEvent:
        item = _mapping(
            value,
            frozenset(("request_id", "sequence", "state", "code", "occurred_at")),
            "ProgressEvent",
        )
        return cls(
            request_id=item["request_id"],
            sequence=item["sequence"],
            state=item["state"],
            code=item["code"],
            occurred_at=item["occurred_at"],
        )


@dataclass(frozen=True)
class CandidateRevisionUpload:
    family_id: str
    revision_id: str
    revision: int
    content: CandidateContent
    content_digest: str
    evidence_digest: str
    provenance: CandidateProvenanceSummary | None = None

    def __post_init__(self) -> None:
        _pattern(self.family_id, _FAMILY_ID, "family_id")
        _pattern(self.revision_id, _REVISION_ID, "revision_id")
        _positive_integer(self.revision, "revision")
        validated_content = _content(self.content)
        expected_content_digest = _sha256(validated_content.to_dict())
        if self.content_digest != expected_content_digest:
            raise ValueError("content_digest does not match Candidate content")
        _pattern(self.evidence_digest, _DIGEST, "evidence_digest")
        expected_revision_id = candidate_revision_id(
            self.family_id,
            self.revision,
            self.content_digest,
        )
        if self.revision_id != expected_revision_id:
            raise ValueError("revision_id does not match Candidate revision")
        if self.provenance is not None and not isinstance(
            self.provenance, CandidateProvenanceSummary
        ):
            raise ValueError("provenance is invalid")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "family_id": self.family_id,
            "revision_id": self.revision_id,
            "revision": self.revision,
            "content": self.content.to_dict(),
            "content_digest": self.content_digest,
            "evidence_digest": self.evidence_digest,
        }
        if self.provenance is not None:
            value["provenance"] = self.provenance.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: object) -> CandidateRevisionUpload:
        legacy_fields = frozenset(
            (
                "family_id",
                "revision_id",
                "revision",
                "content",
                "content_digest",
                "evidence_digest",
            )
        )
        if not isinstance(value, Mapping):
            raise ValueError("CandidateRevisionUpload is invalid")
        fields = frozenset(value)
        if fields not in (legacy_fields, legacy_fields | {"provenance"}):
            raise ValueError("CandidateRevisionUpload fields are invalid")
        item = value
        content = _content(item["content"])
        return cls(
            family_id=item["family_id"],
            revision_id=item["revision_id"],
            revision=item["revision"],
            content=content,
            content_digest=item["content_digest"],
            evidence_digest=item["evidence_digest"],
            provenance=(
                None
                if "provenance" not in item
                else CandidateProvenanceSummary.from_dict(item["provenance"])
            ),
        )


@dataclass(frozen=True)
class CandidateBatchUpload:
    request_id: str
    repository_id: str
    items: tuple[CandidateRevisionUpload, ...]
    batch_digest: str

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        if (
            not isinstance(self.items, tuple)
            or len(self.items) > _MAX_ITEMS
            or any(not isinstance(item, CandidateRevisionUpload) for item in self.items)
        ):
            raise ValueError("Candidate batch items are invalid")
        if any(item.provenance is not None for item in self.items):
            raise ValueError(
                "Legacy Candidate batch cannot contain provenance"
            )
        serialized_items = []
        for item in self.items:
            serialized = item.to_dict()
            if len(canonical_json_bytes(serialized)) > _MAX_ITEM_BYTES:
                raise ValueError("Candidate revision exceeds 16 KiB")
            serialized_items.append(serialized)
        expected_digest = _sha256({"items": serialized_items})
        if self.batch_digest != expected_digest:
            raise ValueError("batch_digest does not match Candidate batch")
        if (
            len(
                canonical_json_bytes(
                    {
                        "request_id": self.request_id,
                        "repository_id": self.repository_id,
                        "items": serialized_items,
                        "batch_digest": self.batch_digest,
                    }
                )
            )
            > _MAX_BATCH_BYTES
        ):
            raise ValueError("Candidate batch exceeds 1 MiB")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "items": [item.to_dict() for item in self.items],
            "batch_digest": self.batch_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> CandidateBatchUpload:
        item = _mapping(
            value,
            frozenset(("request_id", "repository_id", "items", "batch_digest")),
            "CandidateBatchUpload",
        )
        raw_items = item["items"]
        if not isinstance(raw_items, list):
            raise ValueError("Candidate batch items are invalid")
        return cls(
            request_id=item["request_id"],
            repository_id=item["repository_id"],
            items=tuple(
                CandidateRevisionUpload.from_dict(member) for member in raw_items
            ),
            batch_digest=item["batch_digest"],
        )


@dataclass(frozen=True)
class CandidateSliceBatchUpload:
    request_id: str
    slice_id: str
    route_id: str
    route_configuration_version: int
    decision_space_id: str
    items: tuple[CandidateRevisionUpload, ...]
    batch_digest: str
    item_protocol: Literal["candidate-provenance-v1"] | None = None

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.slice_id, _SLICE_ID, "slice_id")
        _pattern(self.route_id, _ROUTE_ID, "route_id")
        _positive_integer(self.route_configuration_version, "route_configuration_version")
        _pattern(self.decision_space_id, _DECISION_SPACE_ID, "decision_space_id")
        if (
            not isinstance(self.items, tuple)
            or len(self.items) > _MAX_ITEMS
            or any(not isinstance(item, CandidateRevisionUpload) for item in self.items)
        ):
            raise ValueError("Candidate slice batch items are invalid")
        if self.item_protocol is None:
            if any(item.provenance is not None for item in self.items):
                raise ValueError(
                    "Legacy Candidate slice items cannot contain provenance"
                )
        elif self.item_protocol == "candidate-provenance-v1":
            if any(item.provenance is None for item in self.items):
                raise ValueError(
                    "v1 Candidate slice items require provenance"
                )
        else:
            raise ValueError("Candidate item protocol is invalid")
        serialized_items: list[dict[str, object]] = []
        for item in self.items:
            serialized = item.to_dict()
            if len(canonical_json_bytes(serialized)) > _MAX_ITEM_BYTES:
                raise ValueError("Candidate revision exceeds 16 KiB")
            serialized_items.append(serialized)
        if self.batch_digest != _sha256({"items": serialized_items}):
            raise ValueError("batch_digest does not match Candidate batch")
        if len(canonical_json_bytes(self.to_dict())) > _MAX_BATCH_BYTES:
            raise ValueError("Candidate batch exceeds 1 MiB")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "request_id": self.request_id,
            "slice_id": self.slice_id,
            "route_id": self.route_id,
            "route_configuration_version": self.route_configuration_version,
            "decision_space_id": self.decision_space_id,
            "items": [item.to_dict() for item in self.items],
            "batch_digest": self.batch_digest,
        }
        if self.item_protocol is not None:
            value["item_protocol"] = self.item_protocol
        return value

    @classmethod
    def from_dict(cls, value: object) -> "CandidateSliceBatchUpload":
        legacy_fields = frozenset(("request_id", "slice_id", "route_id", "route_configuration_version", "decision_space_id", "items", "batch_digest"))
        if not isinstance(value, Mapping):
            raise ValueError("CandidateSliceBatchUpload is invalid")
        fields = frozenset(value)
        if fields not in (legacy_fields, legacy_fields | {"item_protocol"}):
            raise ValueError("CandidateSliceBatchUpload fields are invalid")
        item = value
        raw_items = item["items"]
        if not isinstance(raw_items, list):
            raise ValueError("Candidate slice batch items are invalid")
        return cls(item["request_id"], item["slice_id"], item["route_id"], item["route_configuration_version"], item["decision_space_id"], tuple(CandidateRevisionUpload.from_dict(member) for member in raw_items), item["batch_digest"], item.get("item_protocol"))


@dataclass(frozen=True)
class SliceUploadReceipt:
    request_id: str
    slice_id: str
    candidate_count: int
    receipt_digest: str

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.slice_id, _SLICE_ID, "slice_id")
        if not isinstance(self.candidate_count, int) or isinstance(self.candidate_count, bool) or self.candidate_count < 0:
            raise ValueError("candidate_count is invalid")
        _pattern(self.receipt_digest, _DIGEST, "receipt_digest")

    def to_dict(self) -> dict[str, object]:
        return {"request_id": self.request_id, "slice_id": self.slice_id, "candidate_count": self.candidate_count, "receipt_digest": self.receipt_digest}

    @classmethod
    def from_dict(cls, value: object) -> "SliceUploadReceipt":
        item = _mapping(value, frozenset(("request_id", "slice_id", "candidate_count", "receipt_digest")), "SliceUploadReceipt")
        return cls(item["request_id"], item["slice_id"], item["candidate_count"], item["receipt_digest"])


@dataclass(frozen=True)
class UploadReceipt:
    request_id: str
    batch_digest: str
    acknowledged_at: str

    def __post_init__(self) -> None:
        _pattern(self.request_id, _REQUEST_ID, "request_id")
        _pattern(self.batch_digest, _DIGEST, "batch_digest")
        _timestamp(self.acknowledged_at, "acknowledged_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "batch_digest": self.batch_digest,
            "acknowledged_at": self.acknowledged_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> UploadReceipt:
        item = _mapping(
            value,
            frozenset(("request_id", "batch_digest", "acknowledged_at")),
            "UploadReceipt",
        )
        return cls(
            request_id=item["request_id"],
            batch_digest=item["batch_digest"],
            acknowledged_at=item["acknowledged_at"],
        )
