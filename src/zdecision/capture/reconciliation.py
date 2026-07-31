"""Strict host-owned Candidate family reconciliation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from zdecision.capture.models import Candidate, CandidateContent
from zdecision.ids import candidate_family_id, candidate_revision_id
from zdecision.jsonio import canonical_json_bytes


CandidateRelation = Literal[
    "same",
    "refine",
    "replace",
    "unrelated",
    "ambiguous",
]

_RELATIONS = frozenset(
    ("same", "refine", "replace", "unrelated", "ambiguous")
)
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(
    r"^cand_[0-9a-f]{32}_(?:0[1-9]|1[0-9]|20)$"
)
_FAMILY_ID = re.compile(r"^cfm_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^crv_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReconciliationDecision:
    observation_id: str
    relation: CandidateRelation
    family_id: str | None
    effective_content: CandidateContent | None

    def __post_init__(self) -> None:
        _pattern(
            self.observation_id,
            _OBSERVATION_ID,
            "observation_id",
        )
        if self.relation not in _RELATIONS:
            raise ValueError("relation is invalid")
        if self.relation == "ambiguous":
            if self.family_id is not None:
                raise ValueError("ambiguous relation cannot select a family")
        else:
            _pattern(self.family_id, _FAMILY_ID, "family_id")
        if self.relation in {"refine", "replace"}:
            if not isinstance(self.effective_content, CandidateContent):
                raise ValueError(
                    "refine and replace require effective_content"
                )
        elif self.effective_content is not None:
            raise ValueError(
                "effective_content is only valid for refine or replace"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "relation": self.relation,
            "family_id": self.family_id,
            "effective_content": (
                None
                if self.effective_content is None
                else self.effective_content.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> "ReconciliationDecision":
        _exact_fields(
            value,
            frozenset(
                (
                    "observation_id",
                    "relation",
                    "family_id",
                    "effective_content",
                )
            ),
            "ReconciliationDecision",
        )
        raw_content = value["effective_content"]
        if raw_content is not None and not isinstance(raw_content, Mapping):
            raise ValueError("effective_content is invalid")
        return cls(
            observation_id=value["observation_id"],
            relation=value["relation"],
            family_id=value["family_id"],
            effective_content=(
                None
                if raw_content is None
                else CandidateContent.from_dict(raw_content)
            ),
        )


@dataclass(frozen=True)
class CandidateFamilyRevision:
    family_id: str
    revision_id: str
    revision: int
    content: CandidateContent
    content_digest: str
    evidence_digest: str
    supersedes_revision_id: str | None

    def __post_init__(self) -> None:
        _pattern(self.family_id, _FAMILY_ID, "family_id")
        _pattern(self.revision_id, _REVISION_ID, "revision_id")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 1
        ):
            raise ValueError("revision is invalid")
        if not isinstance(self.content, CandidateContent):
            raise ValueError("content is invalid")
        expected_content_digest = _sha256(self.content.to_dict())
        if self.content_digest != expected_content_digest:
            raise ValueError("content_digest does not match content")
        _pattern(self.evidence_digest, _DIGEST, "evidence_digest")
        expected_revision_id = candidate_revision_id(
            self.family_id,
            self.revision,
            self.content_digest,
        )
        if self.revision_id != expected_revision_id:
            raise ValueError("revision_id does not match revision")
        if self.revision == 1:
            if self.supersedes_revision_id is not None:
                raise ValueError(
                    "initial revision cannot supersede another revision"
                )
        else:
            _pattern(
                self.supersedes_revision_id,
                _REVISION_ID,
                "supersedes_revision_id",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "family_id": self.family_id,
            "revision_id": self.revision_id,
            "revision": self.revision,
            "content": self.content.to_dict(),
            "content_digest": self.content_digest,
            "evidence_digest": self.evidence_digest,
            "supersedes_revision_id": self.supersedes_revision_id,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> "CandidateFamilyRevision":
        _exact_fields(
            value,
            frozenset(
                (
                    "family_id",
                    "revision_id",
                    "revision",
                    "content",
                    "content_digest",
                    "evidence_digest",
                    "supersedes_revision_id",
                )
            ),
            "CandidateFamilyRevision",
        )
        raw_content = value["content"]
        if not isinstance(raw_content, Mapping):
            raise ValueError("content is invalid")
        return cls(
            family_id=value["family_id"],
            revision_id=value["revision_id"],
            revision=value["revision"],
            content=CandidateContent.from_dict(raw_content),
            content_digest=value["content_digest"],
            evidence_digest=value["evidence_digest"],
            supersedes_revision_id=value["supersedes_revision_id"],
        )


@dataclass(frozen=True)
class ReconciliationResult:
    repository_id: str
    current_revisions: tuple[CandidateFamilyRevision, ...]
    new_revisions: tuple[CandidateFamilyRevision, ...]
    uploadable_revisions: tuple[CandidateFamilyRevision, ...]
    same_observation_ids: tuple[str, ...]
    ambiguous_observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _pattern(self.repository_id, _REPOSITORY_ID, "repository_id")
        for field_name in (
            "current_revisions",
            "new_revisions",
            "uploadable_revisions",
        ):
            revisions = getattr(self, field_name)
            if not isinstance(revisions, tuple) or any(
                not isinstance(item, CandidateFamilyRevision)
                for item in revisions
            ):
                raise ValueError(f"{field_name} is invalid")
        current_families = [
            item.family_id for item in self.current_revisions
        ]
        if (
            len(set(current_families)) != len(current_families)
            or tuple(current_families) != tuple(sorted(current_families))
        ):
            raise ValueError("current_revisions are invalid")
        new_keys = [
            (item.family_id, item.revision)
            for item in self.new_revisions
        ]
        if (
            len(set(new_keys)) != len(new_keys)
            or tuple(new_keys) != tuple(sorted(new_keys))
        ):
            raise ValueError("new_revisions are invalid")
        current_by_id = {
            item.revision_id: item for item in self.current_revisions
        }
        new_by_id = {
            item.revision_id: item for item in self.new_revisions
        }
        if len(new_by_id) != len(self.new_revisions):
            raise ValueError("new_revisions repeat a revision id")
        upload_ids = [
            item.revision_id for item in self.uploadable_revisions
        ]
        if (
            len(set(upload_ids)) != len(upload_ids)
            or tuple(
                item.family_id for item in self.uploadable_revisions
            )
            != tuple(sorted(
                item.family_id for item in self.uploadable_revisions
            ))
            or any(
                revision_id not in current_by_id
                or revision_id not in new_by_id
                for revision_id in upload_ids
            )
        ):
            raise ValueError("uploadable_revisions are invalid")
        _observation_ids(
            self.same_observation_ids, "same_observation_ids"
        )
        _observation_ids(
            self.ambiguous_observation_ids,
            "ambiguous_observation_ids",
        )
        if set(self.same_observation_ids) & set(
            self.ambiguous_observation_ids
        ):
            raise ValueError("observation result sets overlap")

    @classmethod
    def empty(cls, repository_id: str) -> "ReconciliationResult":
        return cls(repository_id, (), (), (), (), ())

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "current_revisions": [
                item.to_dict() for item in self.current_revisions
            ],
            "new_revisions": [
                item.to_dict() for item in self.new_revisions
            ],
            "uploadable_revisions": [
                item.to_dict() for item in self.uploadable_revisions
            ],
            "same_observation_ids": list(
                self.same_observation_ids
            ),
            "ambiguous_observation_ids": list(
                self.ambiguous_observation_ids
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> "ReconciliationResult":
        _exact_fields(
            value,
            frozenset(
                (
                    "repository_id",
                    "current_revisions",
                    "new_revisions",
                    "uploadable_revisions",
                    "same_observation_ids",
                    "ambiguous_observation_ids",
                )
            ),
            "ReconciliationResult",
        )
        return cls(
            repository_id=value["repository_id"],
            current_revisions=_revision_list(
                value["current_revisions"], "current_revisions"
            ),
            new_revisions=_revision_list(
                value["new_revisions"], "new_revisions"
            ),
            uploadable_revisions=_revision_list(
                value["uploadable_revisions"],
                "uploadable_revisions",
            ),
            same_observation_ids=_string_tuple(
                value["same_observation_ids"],
                "same_observation_ids",
            ),
            ambiguous_observation_ids=_string_tuple(
                value["ambiguous_observation_ids"],
                "ambiguous_observation_ids",
            ),
        )


def reconciliation_output_schema(
    *,
    observation_ids: tuple[str, ...],
    family_ids: tuple[str, ...],
) -> dict[str, object]:
    observations = _bounded_enum(
        observation_ids, _OBSERVATION_ID, "observation_ids"
    )
    if not isinstance(family_ids, tuple) or not family_ids:
        raise ValueError("family_ids is invalid")
    families = sorted({
        _pattern(value, _FAMILY_ID, "family_ids")
        for value in family_ids
    })
    content_schema = {
        "type": "object",
        "properties": {
            "product": {"type": "string"},
            "claim": {"type": "string"},
            "future_action": {"type": "string"},
            "scope_summary": {"type": "string"},
            "repositories": {
                "type": "array",
                "items": {"type": "string"},
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
            },
            "invalidation_conditions": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "product",
            "claim",
            "future_action",
            "scope_summary",
            "repositories",
            "paths",
            "invalidation_conditions",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "observation_id": {
                            "type": "string",
                            "enum": observations,
                        },
                        "relation": {
                            "type": "string",
                            "enum": sorted(_RELATIONS),
                        },
                        "family_id": {
                            "anyOf": [
                                {
                                    "type": "string",
                                    "enum": families,
                                },
                                {"type": "null"},
                            ]
                        },
                        "effective_content": {
                            "anyOf": [
                                content_schema,
                                {"type": "null"},
                            ]
                        },
                    },
                    "required": [
                        "observation_id",
                        "relation",
                        "family_id",
                        "effective_content",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def validate_reconciliation(
    value: object,
    observations: Sequence[Candidate],
    current: Sequence[CandidateFamilyRevision],
) -> tuple[ReconciliationDecision, ...]:
    ordered = _ordered_observations(observations)
    _current_map(current)
    if (
        not isinstance(value, Mapping)
        or frozenset(value) != frozenset(("results",))
        or not isinstance(value["results"], list)
        or len(value["results"]) != len(ordered)
    ):
        raise ValueError("Reconciliation output is invalid")
    decisions: list[ReconciliationDecision] = []
    for observation_value, raw_result in zip(
        ordered, value["results"], strict=True
    ):
        if not isinstance(raw_result, Mapping):
            raise ValueError("Reconciliation output is invalid")
        decision = ReconciliationDecision.from_dict(raw_result)
        if decision.observation_id != observation_value.candidate_id:
            raise ValueError(
                "Reconciliation results are not in observation order"
            )
        decisions.append(decision)
    return tuple(decisions)


def apply_reconciliation(
    repository_id: str,
    observations: Sequence[Candidate],
    current: Sequence[CandidateFamilyRevision],
    decisions: Sequence[ReconciliationDecision],
) -> ReconciliationResult:
    repository = _pattern(
        repository_id, _REPOSITORY_ID, "repository_id"
    )
    ordered = _ordered_observations(observations)
    if (
        not isinstance(decisions, Sequence)
        or isinstance(decisions, (str, bytes))
        or len(decisions) != len(ordered)
        or any(
            not isinstance(item, ReconciliationDecision)
            for item in decisions
        )
    ):
        raise ValueError("decisions are invalid")
    heads = _current_map(current)
    new_revisions: list[CandidateFamilyRevision] = []
    same_ids: list[str] = []
    ambiguous_ids: list[str] = []
    changed_families: set[str] = set()

    for observation_value, decision in zip(
        ordered, decisions, strict=True
    ):
        if decision.observation_id != observation_value.candidate_id:
            raise ValueError(
                "Decision does not match the ordered observation"
            )
        proposed_family = candidate_family_id(
            repository, observation_value.candidate_id
        )
        if decision.relation == "ambiguous":
            ambiguous_ids.append(observation_value.candidate_id)
            continue
        assert decision.family_id is not None
        if decision.relation == "unrelated":
            if (
                decision.family_id != proposed_family
                or decision.family_id in heads
            ):
                raise ValueError(
                    "Unrelated observation selected an invalid family"
                )
            revision = _new_revision(
                family_id=decision.family_id,
                revision=1,
                content=observation_value.content,
                observation=observation_value,
                supersedes=None,
            )
            heads[decision.family_id] = revision
            new_revisions.append(revision)
            changed_families.add(decision.family_id)
            continue
        if decision.family_id not in heads:
            raise ValueError(
                "Decision selected an unavailable or forward family"
            )
        if decision.relation == "same":
            same_ids.append(observation_value.candidate_id)
            continue
        previous = heads[decision.family_id]
        assert decision.effective_content is not None
        revision = _new_revision(
            family_id=decision.family_id,
            revision=previous.revision + 1,
            content=decision.effective_content,
            observation=observation_value,
            supersedes=previous.revision_id,
        )
        heads[decision.family_id] = revision
        new_revisions.append(revision)
        changed_families.add(decision.family_id)

    current_revisions = tuple(
        heads[family_id] for family_id in sorted(heads)
    )
    ordered_new = tuple(sorted(
        new_revisions,
        key=lambda item: (item.family_id, item.revision),
    ))
    uploadable = tuple(
        heads[family_id] for family_id in sorted(changed_families)
    )
    return ReconciliationResult(
        repository_id=repository,
        current_revisions=current_revisions,
        new_revisions=ordered_new,
        uploadable_revisions=uploadable,
        same_observation_ids=tuple(same_ids),
        ambiguous_observation_ids=tuple(ambiguous_ids),
    )


def _new_revision(
    *,
    family_id: str,
    revision: int,
    content: CandidateContent,
    observation: Candidate,
    supersedes: str | None,
) -> CandidateFamilyRevision:
    content_digest = _sha256(content.to_dict())
    evidence_digest = _sha256(
        {"observation": observation.to_dict()}
    )
    return CandidateFamilyRevision(
        family_id=family_id,
        revision_id=candidate_revision_id(
            family_id, revision, content_digest
        ),
        revision=revision,
        content=content,
        content_digest=content_digest,
        evidence_digest=evidence_digest,
        supersedes_revision_id=supersedes,
    )


def _ordered_observations(
    observations: Sequence[Candidate],
) -> tuple[Candidate, ...]:
    if (
        not isinstance(observations, Sequence)
        or isinstance(observations, (str, bytes))
        or any(not isinstance(item, Candidate) for item in observations)
    ):
        raise ValueError("observations are invalid")
    ordered = tuple(sorted(
        observations, key=lambda item: item.candidate_id
    ))
    ids = [item.candidate_id for item in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("observations contain duplicate ids")
    return ordered


def _current_map(
    current: Sequence[CandidateFamilyRevision],
) -> dict[str, CandidateFamilyRevision]:
    if (
        not isinstance(current, Sequence)
        or isinstance(current, (str, bytes))
        or any(
            not isinstance(item, CandidateFamilyRevision)
            for item in current
        )
    ):
        raise ValueError("current revisions are invalid")
    result: dict[str, CandidateFamilyRevision] = {}
    for item in current:
        if item.family_id in result:
            raise ValueError("current revisions repeat a family")
        result[item.family_id] = item
    return result


def _bounded_enum(
    values: tuple[str, ...],
    pattern: re.Pattern[str],
    field_name: str,
) -> list[str]:
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{field_name} is invalid")
    result = [
        _pattern(value, pattern, field_name) for value in values
    ]
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} contains duplicates")
    return result


def _pattern(
    value: object,
    pattern: re.Pattern[str],
    field_name: str,
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    name: str,
) -> None:
    if frozenset(value) != expected:
        raise ValueError(f"{name} fields are invalid")


def _revision_list(
    value: object, field_name: str
) -> tuple[CandidateFamilyRevision, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} is invalid")
    return tuple(
        CandidateFamilyRevision.from_dict(item)
        if isinstance(item, Mapping)
        else _raise_invalid(field_name)
        for item in value
    )


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{field_name} is invalid")
    return tuple(value)


def _observation_ids(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} is invalid")
    for value in values:
        _pattern(value, _OBSERVATION_ID, field_name)
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} contains duplicates")


def _raise_invalid(field_name: str):
    raise ValueError(f"{field_name} is invalid")
