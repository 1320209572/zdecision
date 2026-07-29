"""Strict private values for immutable Decision publication batches."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal

from zdecision.capture.reviews import ApprovalRef
from zdecision.ids import (
    PUBLISHER_FORMAT_VERSION,
    canonical_product_name,
    decision_id as derive_decision_id,
    product_id as derive_product_id,
    publication_preview_id,
)
from zdecision.jsonio import canonical_json_bytes


PublicationState = Literal[
    "previewed",
    "confirmed",
    "committed_pending_push",
    "completed",
]

_PREVIEW_ID = re.compile(r"^pub_[0-9a-f]{32}$")
_REVIEW_BATCH_ID = re.compile(r"^rvb_[0-9a-f]{32}$")
_REVIEW_ID = re.compile(r"^rvi_[0-9a-f]{32}$")
_CANDIDATE_ID = re.compile(
    r"^cand_[0-9a-f]{32}_(?:0[1-9]|1[0-9]|20)$"
)
_DECISION_ID = re.compile(r"^dec_[0-9a-f]{32}$")
_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STATES = frozenset(
    ("previewed", "confirmed", "committed_pending_push", "completed")
)


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


def _id(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _utc_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field_name} must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError(
            f"{field_name} must be a UTC RFC 3339 timestamp"
        ) from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be a UTC RFC 3339 timestamp")
    return value


def _formal_path(value: object, field_name: str) -> str:
    path = _text(value, field_name)
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or not pure.parts
        or pure.parts[0] != "decision-registry"
        or any(part in ("", ".", "..") for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise ValueError(f"{field_name} is invalid")
    return path


@dataclass(frozen=True)
class PublicationFile:
    """One exact UTF-8 formal document frozen in private state."""

    path: str
    content: str
    sha256: str

    def __post_init__(self) -> None:
        _formal_path(self.path, "Publication file path")
        if not isinstance(self.content, str):
            raise ValueError("Publication file content must be UTF-8 text")
        encoded = self.content.encode("utf-8")
        if _DIGEST.fullmatch(self.sha256) is None:
            raise ValueError("Publication file digest is invalid")
        if hashlib.sha256(encoded).hexdigest() != self.sha256:
            raise ValueError("Publication file digest does not match its content")

    @classmethod
    def from_bytes(cls, path: str, content: bytes) -> PublicationFile:
        if not isinstance(content, bytes):
            raise TypeError("Publication file content must be bytes")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Publication file content must be UTF-8") from None
        return cls(
            path=path,
            content=text,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "content": self.content,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PublicationFile:
        _require_fields(
            value,
            frozenset(("path", "content", "sha256")),
            "PublicationFile",
        )
        content = value["content"]
        if not isinstance(content, str):
            raise ValueError("Publication file content must be a string")
        return cls(
            path=_formal_path(value["path"], "Publication file path"),
            content=content,
            sha256=_id(value["sha256"], _DIGEST, "Publication file digest"),
        )


def _normalized_files(
    files: Sequence[PublicationFile],
    field_name: str,
) -> tuple[PublicationFile, ...]:
    if isinstance(files, (str, bytes)) or not isinstance(files, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    if not files or any(not isinstance(file, PublicationFile) for file in files):
        raise ValueError(f"{field_name} must contain Publication files")
    ordered = tuple(sorted(files, key=lambda file: file.path))
    if len({file.path for file in ordered}) != len(ordered):
        raise ValueError(f"{field_name} contains a duplicate path")
    return ordered


def content_digest_for_files(files: Sequence[PublicationFile]) -> str:
    """Bind every exact display path and UTF-8 content byte in stable order."""

    ordered = _normalized_files(files, "Publication documents")
    payload = {
        "documents": [
            {"content": file.content, "path": file.path} for file in ordered
        ]
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _files_from_json(value: list[object], field_name: str) -> tuple[PublicationFile, ...]:
    files: list[PublicationFile] = []
    for member in value:
        if not isinstance(member, Mapping):
            raise ValueError(f"{field_name} contains an invalid file")
        files.append(PublicationFile.from_dict(member))
    return tuple(files)


@dataclass(frozen=True)
class PublicationRecord:
    """One immutable preview plus its monotonic private publication state."""

    record_version: int
    preview_id: str
    content_digest: str
    state: PublicationState
    created_at: str
    review_batch_id: str
    review_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    product_id: str
    product_name: str
    base_commit: str
    base_registry_digests: Mapping[str, str]
    display_documents: tuple[PublicationFile, ...]
    changed_files: tuple[PublicationFile, ...]
    commit_message: str
    publication_approval: ApprovalRef | None = None
    commit_sha: str | None = None

    def __post_init__(self) -> None:
        if self.record_version != 1 or isinstance(self.record_version, bool):
            raise ValueError("Publication record version is invalid")
        _id(self.preview_id, _PREVIEW_ID, "Publication preview id")
        _id(self.content_digest, _DIGEST, "Publication content digest")
        if self.state not in _STATES:
            raise ValueError("Publication state is invalid")
        _utc_timestamp(self.created_at, "Publication created_at")
        _id(self.review_batch_id, _REVIEW_BATCH_ID, "Review batch id")

        review_ids = self._validated_ids(
            self.review_ids, _REVIEW_ID, "Review ids"
        )
        candidate_ids = self._validated_ids(
            self.candidate_ids, _CANDIDATE_ID, "Candidate ids"
        )
        decision_ids = self._validated_ids(
            self.decision_ids, _DECISION_ID, "Decision ids"
        )
        if not (
            len(review_ids) == len(candidate_ids) == len(decision_ids)
        ):
            raise ValueError("Publication item identity lengths do not match")

        _id(self.product_id, _PRODUCT_ID, "Product id")
        if (
            canonical_product_name(self.product_name) != self.product_name
            or derive_product_id(self.product_name) != self.product_id
        ):
            raise ValueError("Publication product identity is invalid")
        for candidate, decision in zip(
            candidate_ids, decision_ids, strict=True
        ):
            if derive_decision_id(candidate, self.product_id) != decision:
                raise ValueError("Publication Decision identity is invalid")

        _id(self.base_commit, _GIT_COMMIT, "Publication base commit")
        digests = self._validated_digests(self.base_registry_digests)
        display = _normalized_files(
            self.display_documents, "Publication display documents"
        )
        changed = _normalized_files(
            self.changed_files, "Publication changed files"
        )
        display_by_path = {file.path: file for file in display}
        if any(display_by_path.get(file.path) != file for file in changed):
            raise ValueError("Changed files are not an exact display subset")
        if content_digest_for_files(display) != self.content_digest:
            raise ValueError("Publication content digest is invalid")

        expected_preview = publication_preview_id(
            {
                "base_commit": self.base_commit,
                "base_registry_digests": digests,
                "decision_ids": decision_ids,
                "publisher_format": PUBLISHER_FORMAT_VERSION,
                "review_ids": review_ids,
                "target_paths": tuple(file.path for file in changed),
            }
        )
        if expected_preview != self.preview_id:
            raise ValueError("Publication preview identity is invalid")
        expected_message = (
            f"decision({self.product_id}): publish {len(decision_ids)} decisions\n\n"
            f"ZDecision-Preview: {self.preview_id}\n"
        )
        if self.commit_message != expected_message:
            raise ValueError("Publication commit message is invalid")

        approval_present = self.publication_approval is not None
        commit_present = self.commit_sha is not None
        expected_shape = {
            "previewed": (False, False),
            "confirmed": (True, False),
            "committed_pending_push": (True, True),
            "completed": (True, True),
        }[self.state]
        if (approval_present, commit_present) != expected_shape:
            raise ValueError("Publication state fields are invalid")
        if approval_present and not isinstance(
            self.publication_approval, ApprovalRef
        ):
            raise ValueError("Publication approval is invalid")
        if commit_present:
            _id(self.commit_sha, _GIT_COMMIT, "Publication commit")

        object.__setattr__(self, "review_ids", review_ids)
        object.__setattr__(self, "candidate_ids", candidate_ids)
        object.__setattr__(self, "decision_ids", decision_ids)
        object.__setattr__(self, "base_registry_digests", digests)
        object.__setattr__(self, "display_documents", display)
        object.__setattr__(self, "changed_files", changed)

    @staticmethod
    def _validated_ids(
        values: tuple[str, ...],
        pattern: re.Pattern[str],
        field_name: str,
    ) -> tuple[str, ...]:
        if not isinstance(values, tuple) or not values:
            raise ValueError(f"{field_name} must be a non-empty tuple")
        if any(not isinstance(value, str) or pattern.fullmatch(value) is None for value in values):
            raise ValueError(f"{field_name} contains an invalid id")
        if len(set(values)) != len(values):
            raise ValueError(f"{field_name} contains a duplicate id")
        return values

    @staticmethod
    def _validated_digests(values: Mapping[str, str]) -> Mapping[str, str]:
        if not isinstance(values, Mapping) or not values:
            raise ValueError("Base Registry digests must be a non-empty object")
        normalized: dict[str, str] = {}
        for path, digest in values.items():
            normalized_path = _formal_path(path, "Base Registry digest path")
            if not isinstance(digest, str) or (
                digest != "missing" and _DIGEST.fullmatch(digest) is None
            ):
                raise ValueError("Base Registry digest is invalid")
            normalized[normalized_path] = digest
        return MappingProxyType(dict(sorted(normalized.items())))

    def display_file_bytes(self) -> dict[str, bytes]:
        return {
            file.path: file.content.encode("utf-8")
            for file in self.display_documents
        }

    def changed_file_bytes(self) -> dict[str, bytes]:
        return {
            file.path: file.content.encode("utf-8") for file in self.changed_files
        }

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "record_version": self.record_version,
            "preview_id": self.preview_id,
            "content_digest": self.content_digest,
            "state": self.state,
            "created_at": self.created_at,
            "review_batch_id": self.review_batch_id,
            "review_ids": list(self.review_ids),
            "candidate_ids": list(self.candidate_ids),
            "decision_ids": list(self.decision_ids),
            "product_id": self.product_id,
            "product_name": self.product_name,
            "base_commit": self.base_commit,
            "base_registry_digests": dict(self.base_registry_digests),
            "display_documents": [
                file.to_dict() for file in self.display_documents
            ],
            "changed_files": [file.to_dict() for file in self.changed_files],
            "commit_message": self.commit_message,
        }
        if self.publication_approval is not None:
            value["publication_approval"] = self.publication_approval.to_dict()
        if self.commit_sha is not None:
            value["commit_sha"] = self.commit_sha
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PublicationRecord:
        base_fields = frozenset(
            (
                "record_version",
                "preview_id",
                "content_digest",
                "state",
                "created_at",
                "review_batch_id",
                "review_ids",
                "candidate_ids",
                "decision_ids",
                "product_id",
                "product_name",
                "base_commit",
                "base_registry_digests",
                "display_documents",
                "changed_files",
                "commit_message",
            )
        )
        allowed = base_fields | frozenset(("publication_approval", "commit_sha"))
        if not isinstance(value, Mapping) or not base_fields.issubset(value):
            raise ValueError("PublicationRecord has invalid fields")
        if not frozenset(value).issubset(allowed):
            raise ValueError("PublicationRecord has invalid fields")
        review_ids = value["review_ids"]
        candidate_ids = value["candidate_ids"]
        decision_ids = value["decision_ids"]
        digests = value["base_registry_digests"]
        display = value["display_documents"]
        changed = value["changed_files"]
        if not all(
            isinstance(member, list)
            for member in (review_ids, candidate_ids, decision_ids, display, changed)
        ) or not isinstance(digests, Mapping):
            raise ValueError("PublicationRecord collections are invalid")
        approval_value = value.get("publication_approval")
        if approval_value is not None and not isinstance(approval_value, Mapping):
            raise ValueError("Publication approval is invalid")
        return cls(
            record_version=value["record_version"],
            preview_id=_id(value["preview_id"], _PREVIEW_ID, "Publication preview id"),
            content_digest=_id(value["content_digest"], _DIGEST, "Publication content digest"),
            state=value["state"],
            created_at=_utc_timestamp(value["created_at"], "Publication created_at"),
            review_batch_id=_id(value["review_batch_id"], _REVIEW_BATCH_ID, "Review batch id"),
            review_ids=tuple(review_ids),
            candidate_ids=tuple(candidate_ids),
            decision_ids=tuple(decision_ids),
            product_id=_id(value["product_id"], _PRODUCT_ID, "Product id"),
            product_name=_text(value["product_name"], "Product name"),
            base_commit=_id(value["base_commit"], _GIT_COMMIT, "Publication base commit"),
            base_registry_digests=dict(digests),
            display_documents=_files_from_json(
                display, "Publication display documents"
            ),
            changed_files=_files_from_json(
                changed, "Publication changed files"
            ),
            commit_message=_text(value["commit_message"], "Publication commit message"),
            publication_approval=(
                ApprovalRef.from_dict(approval_value)
                if approval_value is not None
                else None
            ),
            commit_sha=(
                _id(value["commit_sha"], _GIT_COMMIT, "Publication commit")
                if "commit_sha" in value
                else None
            ),
        )


@dataclass(frozen=True)
class CandidatePublicationReceipt:
    candidate_id: str
    decision_id: str
    product_id: str
    preview_id: str
    commit_sha: str
    recorded_at: str

    def __post_init__(self) -> None:
        _id(self.candidate_id, _CANDIDATE_ID, "Receipt Candidate id")
        _id(self.product_id, _PRODUCT_ID, "Receipt product id")
        _id(self.decision_id, _DECISION_ID, "Receipt Decision id")
        if derive_decision_id(self.candidate_id, self.product_id) != self.decision_id:
            raise ValueError("Candidate publication receipt identity is invalid")
        _id(self.preview_id, _PREVIEW_ID, "Receipt preview id")
        _id(self.commit_sha, _GIT_COMMIT, "Receipt commit")
        _utc_timestamp(self.recorded_at, "Receipt recorded_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "decision_id": self.decision_id,
            "product_id": self.product_id,
            "preview_id": self.preview_id,
            "commit_sha": self.commit_sha,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> CandidatePublicationReceipt:
        _require_fields(
            value,
            frozenset(
                (
                    "candidate_id",
                    "decision_id",
                    "product_id",
                    "preview_id",
                    "commit_sha",
                    "recorded_at",
                )
            ),
            "CandidatePublicationReceipt",
        )
        return cls(
            candidate_id=_id(value["candidate_id"], _CANDIDATE_ID, "Receipt Candidate id"),
            decision_id=_id(value["decision_id"], _DECISION_ID, "Receipt Decision id"),
            product_id=_id(value["product_id"], _PRODUCT_ID, "Receipt product id"),
            preview_id=_id(value["preview_id"], _PREVIEW_ID, "Receipt preview id"),
            commit_sha=_id(value["commit_sha"], _GIT_COMMIT, "Receipt commit"),
            recorded_at=_utc_timestamp(value["recorded_at"], "Receipt recorded_at"),
        )


@dataclass(frozen=True)
class PublicationResult:
    preview_id: str
    decision_ids: tuple[str, ...]
    status: Literal["committed_pending_push", "completed"]
    commit_sha: str

    def __post_init__(self) -> None:
        _id(self.preview_id, _PREVIEW_ID, "Publication result preview id")
        PublicationRecord._validated_ids(
            self.decision_ids, _DECISION_ID, "Publication result Decision ids"
        )
        if self.status not in ("committed_pending_push", "completed"):
            raise ValueError("Publication result status is invalid")
        _id(self.commit_sha, _GIT_COMMIT, "Publication result commit")
