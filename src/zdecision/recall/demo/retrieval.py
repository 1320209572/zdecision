"""Bounded weighted fusion and local reranking for the recall demo."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from zdecision.recall.session import RecallIntent
from zdecision.registry.models import DecisionRevision

from zdecision.recall.demo.bundle import VerifiedDemoBundle
from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.index import ChannelHit, DemoIndex, DemoIndexError
from zdecision.recall.demo.projection import (
    ProjectedDecision,
    project_decision,
    project_query,
)
from zdecision.recall.demo.runtime import ModelRuntimeBundle


class DemoRetrievalError(RuntimeError):
    """A deliberately non-sensitive retrieval failure."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RankedDemoDecision:
    revision: DecisionRevision
    digest: str
    reranker_score: float
    fused_score: float
    match_reason: str


@dataclass(frozen=True)
class DemoRecallResult:
    intent_digest: str
    profile_digest: str
    manifest_digest: str
    items: tuple[RankedDemoDecision, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_digest": self.intent_digest,
            "profile_digest": self.profile_digest,
            "manifest_digest": self.manifest_digest,
            "items": [
                {
                    "decision": item.revision.to_dict(),
                    "digest": item.digest,
                    "reranker_score": item.reranker_score,
                    "fused_score": item.fused_score,
                    "match_reason": item.match_reason,
                }
                for item in self.items
            ],
        }


class HybridDemoRetriever:
    def retrieve(
        self,
        intent: RecallIntent,
        bundle: VerifiedDemoBundle,
        index: DemoIndex,
        runtime: ModelRuntimeBundle,
    ) -> DemoRecallResult:
        documents = _validate_bindings(intent, bundle, index, runtime)
        profile = bundle.profile
        query = project_query(intent)
        try:
            raw_query_vector = runtime.embedding.embed_query(query.embedding_text)
        except Exception:
            raise DemoRetrievalError("query_embedding_failed") from None
        try:
            query_vector = _freeze_vector(
                raw_query_vector, profile.embedding_dimension
            )
        except Exception:
            raise DemoRetrievalError("query_embedding_invalid") from None
        if query_vector is None:
            raise DemoRetrievalError("query_embedding_invalid")

        try:
            lexical = index.bm25_search(query, profile.bm25_depth)
            semantic = index.dense_search(query_vector, profile.dense_depth)
            path = index.path_search(query, profile.path_depth)
        except DemoIndexError:
            raise DemoRetrievalError("retrieval_channel_invalid") from None
        known_ids = frozenset(documents)
        _validate_hits(lexical, profile.bm25_depth, known_ids)
        _validate_hits(semantic, profile.dense_depth, known_ids)
        _validate_hits(path, profile.path_depth, known_ids)

        channels = (
            ("lexical", lexical, profile.bm25_weight),
            ("semantic", semantic, profile.dense_weight),
            ("path", path, profile.path_weight),
        )
        fused: dict[str, float] = {}
        memberships: dict[str, set[str]] = {}
        constant = profile.reciprocal_rank_constant
        for name, hits, weight in channels:
            for rank, hit in enumerate(hits, start=1):
                memberships.setdefault(hit.decision_id, set()).add(name)
                fused[hit.decision_id] = fused.get(hit.decision_id, 0.0) + weight / (
                    constant + rank
                )
        ordered_ids = sorted(
            (decision_id for decision_id, score in fused.items() if score > 0.0),
            key=lambda decision_id: (
                -fused[decision_id],
                decision_id,
                documents[decision_id].revision.revision,
                _digest(documents[decision_id]),
            ),
        )[: profile.union_depth]
        rerank_ids = ordered_ids[: profile.rerank_depth]
        rerank_documents = tuple(
            documents[decision_id].reranker_text for decision_id in rerank_ids
        )
        try:
            raw_scores = runtime.reranker.score(
                query.reranker_text, rerank_documents
            )
        except Exception:
            raise DemoRetrievalError("reranker_failed") from None
        try:
            scores = _freeze_scores(raw_scores, len(rerank_ids))
        except Exception:
            raise DemoRetrievalError("reranker_output_invalid") from None
        if scores is None:
            raise DemoRetrievalError("reranker_output_invalid")

        eligible = [
            (decision_id, score)
            for decision_id, score in zip(rerank_ids, scores, strict=True)
            if score >= profile.reranker_threshold
        ]
        eligible.sort(key=lambda item: (-item[1], -fused[item[0]], item[0]))

        packed: list[RankedDemoDecision] = []
        packed_bytes = 0
        for decision_id, reranker_score in eligible:
            if len(packed) >= profile.max_shortlist_items:
                break
            document = documents[decision_id]
            size = len(document.canonical_bytes)
            if size > profile.max_shortlist_utf8_bytes - packed_bytes:
                continue
            packed.append(
                RankedDemoDecision(
                    revision=document.revision,
                    digest=_digest(document),
                    reranker_score=reranker_score,
                    fused_score=fused[decision_id],
                    match_reason=_match_reason(memberships[decision_id]),
                )
            )
            packed_bytes += size
        return DemoRecallResult(
            intent_digest=intent.digest,
            profile_digest=profile.digest,
            manifest_digest=bundle.manifest_digest,
            items=tuple(packed),
        )


def _validate_bindings(
    intent: RecallIntent,
    bundle: VerifiedDemoBundle,
    index: DemoIndex,
    runtime: ModelRuntimeBundle,
) -> dict[str, ProjectedDecision]:
    profile = bundle.profile
    try:
        DemoRetrievalProfile.from_dict(profile.to_dict())
    except (TypeError, ValueError):
        raise DemoRetrievalError("profile_invalid") from None
    if (
        intent.explicit_multi_space
        or intent.target_decision_space_ids != (bundle.decision_space_id,)
        or bundle.decision_space_id != profile.decision_space_id
        or bundle.product_name != profile.product_name
        or bundle.repository != profile.repository
        or index.decision_space_id != bundle.decision_space_id
        or index.profile_digest != profile.digest
        or index.manifest_digest != bundle.manifest_digest
        or runtime.profile_digest != profile.digest
        or runtime.embedding.dimension != profile.embedding_dimension
    ):
        raise DemoRetrievalError("retrieval_binding_invalid")
    if len(index.documents) != len(index.vectors) or not index.documents:
        raise DemoRetrievalError("index_invalid")
    if any(
        not _valid_vector(vector, profile.embedding_dimension)
        for vector in index.vectors
    ):
        raise DemoRetrievalError("index_invalid")

    bundle_documents: dict[tuple[str, int], ProjectedDecision] = {}
    for revision in bundle.decisions:
        if (
            revision.product_id != bundle.decision_space_id
            or revision.product_name != bundle.product_name
            or revision.lifecycle != "active"
            or not isinstance(revision.revision, int)
            or isinstance(revision.revision, bool)
            or revision.revision <= 0
            or revision.repositories != (bundle.repository,)
        ):
            raise DemoRetrievalError("decision_leaf_invalid")
        document = project_decision(revision)
        identity = (revision.decision_id, revision.revision)
        if identity in bundle_documents:
            raise DemoRetrievalError("decision_identity_duplicate")
        bundle_documents[identity] = document

    indexed_documents: dict[tuple[str, int], ProjectedDecision] = {}
    for document in index.documents:
        identity = (document.revision.decision_id, document.revision.revision)
        if identity in indexed_documents:
            raise DemoRetrievalError("decision_identity_duplicate")
        indexed_documents[identity] = document
    if frozenset(indexed_documents) != frozenset(bundle_documents):
        raise DemoRetrievalError("index_manifest_mismatch")
    for identity, document in indexed_documents.items():
        expected = bundle_documents[identity]
        if document != expected:
            raise DemoRetrievalError("decision_identity_conflict")
    return {
        identity[0]: indexed_documents[identity]
        for identity in sorted(indexed_documents)
    }


def _validate_hits(
    hits: tuple[ChannelHit, ...], depth: int, known_ids: frozenset[str]
) -> None:
    if len(hits) > depth:
        raise DemoRetrievalError("retrieval_channel_invalid")
    seen: set[str] = set()
    for hit in hits:
        if (
            hit.decision_id not in known_ids
            or hit.decision_id in seen
            or not isinstance(hit.score, float)
            or not math.isfinite(hit.score)
        ):
            raise DemoRetrievalError("retrieval_channel_invalid")
        seen.add(hit.decision_id)


def _freeze_vector(value: object, dimension: int) -> tuple[float, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    vector = tuple(value)
    return vector if _valid_vector(vector, dimension) else None


def _valid_vector(vector: object, dimension: int) -> bool:
    if not isinstance(vector, tuple) or len(vector) != dimension or any(
        not isinstance(value, float) or not math.isfinite(value) for value in vector
    ):
        return False
    norm = math.sqrt(sum(value * value for value in vector))
    return math.isfinite(norm) and abs(norm - 1.0) <= 1e-4


def _freeze_scores(value: object, count: int) -> tuple[float, ...] | None:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        return None
    scores = tuple(value)
    if any(
        not isinstance(score, float) or not math.isfinite(score) for score in scores
    ):
        return None
    return scores


def _digest(document: ProjectedDecision) -> str:
    return hashlib.sha256(document.canonical_bytes).hexdigest()


def _match_reason(membership: set[str]) -> str:
    names = tuple(
        name for name in ("semantic", "lexical", "path") if name in membership
    )
    if not names:
        raise DemoRetrievalError("retrieval_channel_invalid")
    return "+".join(names)
