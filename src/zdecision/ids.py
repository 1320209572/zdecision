"""Stable identifiers owned by ZDecision."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from zdecision.jsonio import canonical_json_bytes

if TYPE_CHECKING:
    from zdecision.capture.templates import TemplateSnapshot


CAPTURE_EXTRACTOR_VERSION = "extractor-v2"
ON_DEMAND_CAPTURE_PROTOCOL = "extractor-v3"
PUBLISHER_FORMAT_VERSION = "zdecision-publisher/v1"

_CAPTURE_ID = re.compile(r"^cap_[0-9a-f]{32}$")
_CANDIDATE_ID = re.compile(
    r"^cand_[0-9a-f]{32}_(?:0[1-9]|1[0-9]|20)$"
)
_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_DECISION_SPACE_ID = re.compile(r"^dsp_[0-9a-f]{32}$")
_CATALOG_GROUP_ID = re.compile(r"^dsg_[0-9a-f]{32}$")
_REPOSITORY_ROUTE_ID = re.compile(r"^drr_[0-9a-f]{32}$")
_REVIEW_BATCH_ID = re.compile(r"^rvb_[0-9a-f]{32}$")
_DECISION_ID = re.compile(r"^dec_[0-9a-f]{32}$")
_REVIEW_ID = re.compile(r"^rvi_[0-9a-f]{32}$")
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_CANDIDATE_FAMILY_ID = re.compile(r"^cfm_[0-9a-f]{32}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_WEB_ACTION_ID = re.compile(r"^web_action_[A-Za-z0-9_-]{1,96}$")
_PREVIEW_ID = re.compile(r"^pub_[0-9a-f]{32}$")
_CONTENT_FIELDS = frozenset(
    (
        "product",
        "claim",
        "future_action",
        "scope_summary",
        "repositories",
        "paths",
        "invalidation_conditions",
    )
)
_SCALAR_CONTENT_FIELDS = (
    "product",
    "claim",
    "future_action",
    "scope_summary",
)
_LIST_CONTENT_FIELDS = (
    "repositories",
    "paths",
    "invalidation_conditions",
)


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}_{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:32]}"


def _nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _normalized_content(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != _CONTENT_FIELDS:
        raise ValueError("effective_content has invalid fields")
    normalized: dict[str, object] = {}
    for field_name in _SCALAR_CONTENT_FIELDS:
        normalized[field_name] = _nonempty_string(value[field_name], field_name)
    for field_name in _LIST_CONTENT_FIELDS:
        members = value[field_name]
        if not isinstance(members, (list, tuple)):
            raise ValueError(f"{field_name} must be a list")
        normalized[field_name] = [
            _nonempty_string(member, field_name) for member in members
        ]
    return normalized


def canonical_product_name(value: str) -> str:
    """Return the path-independent canonical product display name."""

    if not isinstance(value, str):
        raise ValueError("product name must be a string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise ValueError("product name must not be empty")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("product name must not contain control characters")
    return normalized


def product_id(canonical_name: str) -> str:
    """Return the stable product identity for one canonical display name."""

    return _stable_id(
        "prod",
        {"product_name": canonical_product_name(canonical_name)},
    )


def decision_space_id(kind: str, compatibility_product_id: str) -> str:
    """Return the stable leaf Decision-space identity."""

    if kind not in ("product", "shared_unit"):
        raise ValueError("kind is invalid")
    if not isinstance(compatibility_product_id, str) or (
        _PRODUCT_ID.fullmatch(compatibility_product_id) is None
    ):
        raise ValueError("compatibility_product_id is invalid")
    return _stable_id(
        "dsp",
        {
            "kind": kind,
            "compatibility_product_id": compatibility_product_id,
        },
    )


def catalog_group_id(breadcrumb: Sequence[str]) -> str:
    """Return the stable identity for a navigation-only catalog group."""

    if isinstance(breadcrumb, (str, bytes)) or not isinstance(
        breadcrumb, Sequence
    ):
        raise ValueError("breadcrumb is invalid")
    normalized = [canonical_product_name(item) for item in breadcrumb]
    if not normalized:
        raise ValueError("breadcrumb is invalid")
    return _stable_id("dsg", {"breadcrumb": normalized})


def repository_route_id(
    repository_id: str, decision_space_id_value: str
) -> str:
    """Return the stable route identity for a repository and one leaf."""

    if not isinstance(repository_id, str) or _REPOSITORY_ID.fullmatch(
        repository_id
    ) is None:
        raise ValueError("repository_id is invalid")
    if not isinstance(decision_space_id_value, str) or (
        _DECISION_SPACE_ID.fullmatch(decision_space_id_value) is None
    ):
        raise ValueError("decision_space_id is invalid")
    return _stable_id(
        "drr",
        {
            "repository_id": repository_id,
            "decision_space_id": decision_space_id_value,
        },
    )


def capture_request_id(
    organization_id: str,
    repository_id: str,
    template_id: str,
    client_action_id: str,
) -> str:
    """Return the replay-stable identity for one browser action."""

    organization = _nonempty_string(organization_id, "organization_id")
    if (
        not isinstance(repository_id, str)
        or _REPOSITORY_ID.fullmatch(repository_id) is None
    ):
        raise ValueError("repository_id is invalid")
    template = _nonempty_string(template_id, "template_id")
    action = _nonempty_string(client_action_id, "client_action_id")
    return _stable_id(
        "crq",
        {
            "client_action_id": action,
            "organization_id": organization,
            "repository_id": repository_id,
            "template_id": template,
        },
    )


def candidate_family_id(
    repository_id: str,
    first_observation_id: str,
) -> str:
    """Return the stable family seeded by its first Candidate observation."""

    if (
        not isinstance(repository_id, str)
        or _REPOSITORY_ID.fullmatch(repository_id) is None
    ):
        raise ValueError("repository_id is invalid")
    if (
        not isinstance(first_observation_id, str)
        or _CANDIDATE_ID.fullmatch(first_observation_id) is None
    ):
        raise ValueError("first_observation_id is invalid")
    return _stable_id(
        "cfm",
        {
            "first_observation_id": first_observation_id,
            "repository_id": repository_id,
        },
    )


def candidate_revision_id(
    family_id: str,
    revision: int,
    content_digest: str,
) -> str:
    """Return the immutable identity for one family revision."""

    if (
        not isinstance(family_id, str)
        or _CANDIDATE_FAMILY_ID.fullmatch(family_id) is None
    ):
        raise ValueError("family_id is invalid")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        raise ValueError("revision must be a positive integer")
    if not isinstance(content_digest, str) or _DIGEST.fullmatch(content_digest) is None:
        raise ValueError("content_digest is invalid")
    return _stable_id(
        "crv",
        {
            "content_digest": content_digest,
            "family_id": family_id,
            "revision": revision,
        },
    )


def publication_candidate_id(family_id: str) -> str:
    """Return the sole V1 publication Candidate identity for a family."""

    if (
        not isinstance(family_id, str)
        or _CANDIDATE_FAMILY_ID.fullmatch(family_id) is None
    ):
        raise ValueError("family_id is invalid")
    return f"cand_{family_id.removeprefix('cfm_')}_01"


def central_review_batch_id(
    organization_id: str,
    actor_id: str,
    product_id_value: str,
    client_action_id: str,
    ordered_items: Sequence[Mapping[str, object]],
) -> str:
    """Return the replay-stable identity for an ordered central Web Review."""

    organization = _nonempty_string(organization_id, "organization_id")
    actor = _nonempty_string(actor_id, "actor_id")
    if (
        not isinstance(product_id_value, str)
        or _PRODUCT_ID.fullmatch(product_id_value) is None
    ):
        raise ValueError("product_id is invalid")
    if (
        not isinstance(client_action_id, str)
        or _WEB_ACTION_ID.fullmatch(client_action_id) is None
    ):
        raise ValueError("client_action_id is invalid")
    if isinstance(ordered_items, (str, bytes)) or not isinstance(
        ordered_items, Sequence
    ):
        raise ValueError("ordered_items must be a sequence")
    if not 1 <= len(ordered_items) <= 20:
        raise ValueError("ordered_items must contain between 1 and 20 items")

    normalized_items: list[dict[str, object]] = []
    seen_families: set[str] = set()
    draft_fields = frozenset(
        (
            "family_id",
            "repository_id",
            "revision_id",
            "revision",
            "content_digest",
            "action",
            "effective_content",
            "note",
        )
    )
    for item in ordered_items:
        if not isinstance(item, Mapping) or frozenset(item) != draft_fields:
            raise ValueError("Central Review identity item has invalid fields")
        family_id = item["family_id"]
        if (
            not isinstance(family_id, str)
            or _CANDIDATE_FAMILY_ID.fullmatch(family_id) is None
        ):
            raise ValueError("family_id is invalid")
        if family_id in seen_families:
            raise ValueError("Central Review identity items contain a duplicate family")
        seen_families.add(family_id)
        repository_id = item["repository_id"]
        if (
            not isinstance(repository_id, str)
            or _REPOSITORY_ID.fullmatch(repository_id) is None
        ):
            raise ValueError("repository_id is invalid")
        revision = item["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("revision is invalid")
        content_digest = item["content_digest"]
        if not isinstance(content_digest, str) or _DIGEST.fullmatch(content_digest) is None:
            raise ValueError("content_digest is invalid")
        revision_id = item["revision_id"]
        if (
            not isinstance(revision_id, str)
            or revision_id != candidate_revision_id(
                family_id, revision, content_digest
            )
        ):
            raise ValueError("revision_id is invalid")
        action = item["action"]
        if action not in ("accept", "edit_accept", "reject", "skip"):
            raise ValueError("Review action is invalid")
        effective_content = item["effective_content"]
        if action == "edit_accept":
            normalized_content: dict[str, object] | None = _normalized_content(
                effective_content
            )
        elif effective_content is not None:
            raise ValueError("Only edit_accept may contain effective_content")
        else:
            normalized_content = None
        note = item["note"]
        if note is not None and not isinstance(note, str):
            raise ValueError("note is invalid")
        normalized_items.append(
            {
                "family_id": family_id,
                "repository_id": repository_id,
                "revision_id": revision_id,
                "revision": revision,
                "content_digest": content_digest,
                "action": action,
                "effective_content": normalized_content,
                "note": note,
            }
        )
    return _stable_id(
        "rvb",
        {
            "actor_id": actor,
            "client_action_id": client_action_id,
            "items": normalized_items,
            "organization_id": organization,
            "product_id": product_id_value,
        },
    )


def central_publication_id(preview_id: str) -> str:
    """Return the stable central-publication identity for one preview."""

    if not isinstance(preview_id, str) or _PREVIEW_ID.fullmatch(preview_id) is None:
        raise ValueError("preview_id is invalid")
    return _stable_id("plb", {"preview_id": preview_id})


def review_batch_id(
    capture_id: str,
    ordered_items: Sequence[Mapping[str, object]],
    approval_thread_id: str,
    approval_turn_id: str,
) -> str:
    """Return the stable identity for one ordered Review batch."""

    if not isinstance(capture_id, str) or _CAPTURE_ID.fullmatch(capture_id) is None:
        raise ValueError("capture_id is invalid")
    thread_id = _nonempty_string(approval_thread_id, "approval_thread_id")
    turn_id = _nonempty_string(approval_turn_id, "approval_turn_id")
    if isinstance(ordered_items, (str, bytes)) or not isinstance(
        ordered_items, Sequence
    ):
        raise ValueError("ordered_items must be a sequence")
    if not 1 <= len(ordered_items) <= 20:
        raise ValueError("ordered_items must contain between 1 and 20 items")

    normalized_items: list[dict[str, object]] = []
    seen_candidates: set[str] = set()
    for item in ordered_items:
        if not isinstance(item, Mapping) or frozenset(item) != frozenset(
            ("candidate_id", "action", "effective_content")
        ):
            raise ValueError("Review identity item has invalid fields")
        candidate_id = _nonempty_string(item["candidate_id"], "candidate_id")
        if _CANDIDATE_ID.fullmatch(candidate_id) is None:
            raise ValueError("candidate_id is invalid")
        if candidate_id in seen_candidates:
            raise ValueError("Review identity items contain a duplicate Candidate")
        seen_candidates.add(candidate_id)
        action = item["action"]
        if action not in ("accept", "edit_accept", "reject", "skip"):
            raise ValueError("Review action is invalid")
        effective_content = item["effective_content"]
        if action in ("accept", "edit_accept"):
            normalized_content: dict[str, object] | None = _normalized_content(
                effective_content
            )
        else:
            if effective_content is not None:
                raise ValueError("Rejected or skipped Review cannot have content")
            normalized_content = None
        normalized_items.append(
            {
                "candidate_id": candidate_id,
                "action": action,
                "effective_content": normalized_content,
            }
        )

    return _stable_id(
        "rvb",
        {
            "approval": {"thread_id": thread_id, "turn_id": turn_id},
            "capture_id": capture_id,
            "items": normalized_items,
        },
    )


def review_item_id(batch_id: str, candidate_id: str) -> str:
    """Return the stable identity for one Candidate result in a Review batch."""

    if not isinstance(batch_id, str) or _REVIEW_BATCH_ID.fullmatch(batch_id) is None:
        raise ValueError("review_batch_id is invalid")
    candidate = _nonempty_string(candidate_id, "candidate_id")
    if _CANDIDATE_ID.fullmatch(candidate) is None:
        raise ValueError("candidate_id is invalid")
    return _stable_id(
        "rvi",
        {"candidate_id": candidate, "review_batch_id": batch_id},
    )


def decision_id(candidate_id: str, product_id_value: str) -> str:
    """Return the one V1 Decision identity owned by a Candidate and product."""

    candidate = _nonempty_string(candidate_id, "candidate_id")
    if _CANDIDATE_ID.fullmatch(candidate) is None:
        raise ValueError("candidate_id is invalid")
    if (
        not isinstance(product_id_value, str)
        or _PRODUCT_ID.fullmatch(product_id_value) is None
    ):
        raise ValueError("product_id is invalid")
    return _stable_id(
        "dec",
        {"candidate_id": candidate, "product_id": product_id_value},
    )


def _validated_id_list(
    value: object,
    pattern: re.Pattern[str],
    field_name: str,
) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    result: list[str] = []
    for member in value:
        if not isinstance(member, str) or pattern.fullmatch(member) is None:
            raise ValueError(f"{field_name} contains an invalid id")
        result.append(member)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} contains a duplicate id")
    return result


def publication_preview_id(payload: Mapping[str, object]) -> str:
    """Return the stable identity for one exact publication base and target set."""

    expected_fields = frozenset(
        (
            "base_commit",
            "base_registry_digests",
            "decision_ids",
            "publisher_format",
            "review_ids",
            "target_paths",
        )
    )
    if not isinstance(payload, Mapping) or frozenset(payload) != expected_fields:
        raise ValueError("publication preview identity has invalid fields")
    base_commit = payload["base_commit"]
    if not isinstance(base_commit, str) or _GIT_COMMIT.fullmatch(base_commit) is None:
        raise ValueError("base_commit is invalid")
    if payload["publisher_format"] != PUBLISHER_FORMAT_VERSION:
        raise ValueError("publisher_format is invalid")
    review_ids = _validated_id_list(payload["review_ids"], _REVIEW_ID, "review_ids")
    decision_ids = _validated_id_list(
        payload["decision_ids"], _DECISION_ID, "decision_ids"
    )
    if len(review_ids) != len(decision_ids):
        raise ValueError("review_ids and decision_ids must have equal length")
    target_paths = payload["target_paths"]
    if not isinstance(target_paths, (list, tuple)) or not target_paths:
        raise ValueError("target_paths must be a non-empty list")
    normalized_paths = [
        _nonempty_string(path, "target_path") for path in target_paths
    ]
    if normalized_paths != sorted(set(normalized_paths)):
        raise ValueError("target_paths must be unique and sorted")
    digests = payload["base_registry_digests"]
    if not isinstance(digests, Mapping) or not digests:
        raise ValueError("base_registry_digests must be a non-empty object")
    normalized_digests: dict[str, str] = {}
    for path, digest in digests.items():
        normalized_path = _nonempty_string(path, "registry digest path")
        if not isinstance(digest, str) or (
            digest != "missing" and _DIGEST.fullmatch(digest) is None
        ):
            raise ValueError("base Registry digest is invalid")
        normalized_digests[normalized_path] = digest

    return _stable_id(
        "pub",
        {
            "base_commit": base_commit,
            "base_registry_digests": normalized_digests,
            "decision_ids": decision_ids,
            "publisher_format": PUBLISHER_FORMAT_VERSION,
            "review_ids": review_ids,
            "target_paths": normalized_paths,
        },
    )


def capture_operation_id(
    source_thread_id: str,
    source_turn_id: str,
    product: str,
    template: TemplateSnapshot,
) -> str:
    """Return the stable identity for one Capture boundary."""

    payload = canonical_json_bytes(
        {
            "extractor_version": CAPTURE_EXTRACTOR_VERSION,
            "product": product,
            "prompt_bundle_sha256": template.prompt_bundle_sha256,
            "source_thread_id": source_thread_id,
            "source_turn_id": source_turn_id,
            "template_id": template.template_id,
            "template_revision": template.revision,
            "template_source_sha256": template.template_source_sha256,
        }
    )
    return f"cap_{hashlib.sha256(payload).hexdigest()[:32]}"


def on_demand_capture_operation_id(frozen_identity: Mapping[str, object]) -> str:
    """Return the stable identity for one extractor-v3 frozen input."""

    if not isinstance(frozen_identity, Mapping):
        raise ValueError("frozen_identity must be an object")
    payload = dict(frozen_identity)
    if payload.get("protocol") is None:
        raise ValueError("frozen_identity protocol is required")
    return _stable_id("cap", payload)


def capture_attempt_id(operation_id: str, generation: int) -> str:
    """Return the deterministic identity for one disposable generation."""

    if not isinstance(operation_id, str) or _CAPTURE_ID.fullmatch(operation_id) is None:
        raise ValueError("operation_id is invalid")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise ValueError("generation must be a positive integer")
    return _stable_id(
        "cat",
        {"generation": generation, "operation_id": operation_id},
    )


def capture_candidate_id(operation_id: str, ordinal: int) -> str:
    """Return the deterministic Candidate id for one V2 Capture ordinal."""

    return f"cand_{operation_id.removeprefix('cap_')}_{ordinal:02d}"
