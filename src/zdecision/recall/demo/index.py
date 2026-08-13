"""Immutable in-memory lexical, dense, and path indexes for the recall demo."""

from __future__ import annotations

import math
import unicodedata
from collections import Counter
from dataclasses import dataclass

from zdecision.recall.demo.bundle import VerifiedDemoBundle
from zdecision.recall.demo.projection import (
    ProjectedDecision,
    ProjectedQuery,
    project_decision,
)
from zdecision.recall.demo.runtime import ModelRuntimeBundle


_K1 = 1.2
_B = 0.75


class DemoIndexError(RuntimeError):
    """A sanitized index construction failure."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ChannelHit:
    decision_id: str
    score: float


@dataclass(frozen=True)
class DemoIndex:
    decision_space_id: str
    profile_digest: str
    documents: tuple[ProjectedDecision, ...]
    vectors: tuple[tuple[float, ...], ...]
    manifest_digest: str

    @classmethod
    def build(
        cls,
        bundle: VerifiedDemoBundle,
        runtime: ModelRuntimeBundle,
    ) -> "DemoIndex":
        profile = bundle.profile
        if (
            bundle.decision_space_id != profile.decision_space_id
            or bundle.product_name != profile.product_name
            or bundle.repository != profile.repository
            or runtime.profile_digest != profile.digest
            or runtime.embedding.dimension != profile.embedding_dimension
            or not bundle.manifest_digest
        ):
            raise DemoIndexError("index_binding_invalid")

        identities: dict[tuple[str, int], bytes] = {}
        documents: list[ProjectedDecision] = []
        for revision in sorted(bundle.decisions, key=lambda item: item.decision_id):
            if (
                revision.product_id != bundle.decision_space_id
                or revision.product_name != bundle.product_name
                or revision.lifecycle != "active"
                or revision.revision != 1
                or revision.repositories != (bundle.repository,)
            ):
                raise DemoIndexError("decision_leaf_invalid")
            projected = project_decision(revision)
            identity = (revision.decision_id, revision.revision)
            previous = identities.get(identity)
            if previous is not None:
                code = (
                    "decision_identity_conflict"
                    if previous != projected.canonical_bytes
                    else "decision_identity_duplicate"
                )
                raise DemoIndexError(code)
            identities[identity] = projected.canonical_bytes
            documents.append(projected)
        if not documents:
            raise DemoIndexError("decision_set_invalid")

        document_tuple = tuple(documents)
        try:
            raw_vectors = runtime.embedding.embed_documents(
                tuple(document.embedding_text for document in document_tuple)
            )
        except Exception:
            raise DemoIndexError("embedding_runtime_failed") from None
        try:
            vectors = _freeze_vectors(
                raw_vectors,
                count=len(document_tuple),
                dimension=profile.embedding_dimension,
            )
        except Exception:
            raise DemoIndexError("embedding_output_invalid") from None
        if vectors is None:
            raise DemoIndexError("embedding_output_invalid")
        return cls(
            decision_space_id=bundle.decision_space_id,
            profile_digest=profile.digest,
            documents=document_tuple,
            vectors=tuple(vectors),
            manifest_digest=bundle.manifest_digest,
        )

    def bm25_search(
        self, query: ProjectedQuery, depth: int
    ) -> tuple[ChannelHit, ...]:
        if depth <= 0 or not query.lexical_tokens:
            return ()
        query_tokens = tuple(dict.fromkeys(query.lexical_tokens))
        frequencies = tuple(
            Counter(document.lexical_tokens) for document in self.documents
        )
        document_count = len(frequencies)
        average_length = sum(sum(items.values()) for items in frequencies) / document_count
        document_frequencies = {
            token: sum(token in items for items in frequencies)
            for token in query_tokens
        }
        hits: list[ChannelHit] = []
        for document, term_frequencies in zip(
            self.documents, frequencies, strict=True
        ):
            length = sum(term_frequencies.values())
            score = 0.0
            for token in query_tokens:
                frequency = term_frequencies.get(token, 0)
                if frequency == 0:
                    continue
                df = document_frequencies[token]
                inverse_frequency = math.log(
                    1.0 + (document_count - df + 0.5) / (df + 0.5)
                )
                denominator = frequency + _K1 * (
                    1.0 - _B + _B * length / average_length
                )
                score += inverse_frequency * frequency * (_K1 + 1.0) / denominator
            if score > 0.0:
                hits.append(ChannelHit(document.revision.decision_id, score))
        hits.sort(key=lambda item: (-item.score, item.decision_id))
        return tuple(hits[:depth])

    def dense_search(
        self, vector: tuple[float, ...], depth: int
    ) -> tuple[ChannelHit, ...]:
        if depth <= 0 or not self.vectors:
            return ()
        if not _valid_vector(vector, len(self.vectors[0])):
            raise DemoIndexError("query_embedding_invalid")
        hits = [
            ChannelHit(
                document.revision.decision_id,
                sum(
                    left * right
                    for left, right in zip(vector, document_vector, strict=True)
                ),
            )
            for document, document_vector in zip(self.documents, self.vectors, strict=True)
        ]
        hits.sort(key=lambda item: (-item.score, item.decision_id))
        return tuple(hits[:depth])

    def path_search(
        self, query: ProjectedQuery, depth: int
    ) -> tuple[ChannelHit, ...]:
        if depth <= 0 or not query.exact_paths:
            return ()
        document_paths = tuple(
            tuple(_normalized_path(path) for path in document.exact_paths)
            for document in self.documents
        )
        full_df: Counter[str] = Counter()
        suffix_df: Counter[str] = Counter()
        basename_df: Counter[str] = Counter()
        for paths in document_paths:
            full_df.update(set(paths))
            basename_df.update({path.split("/")[-1] for path in paths})
            suffix_df.update(
                {
                    "/".join(parts[index:])
                    for path in paths
                    for parts in (path.split("/"),)
                    for index in range(len(parts) - 1)
                }
            )

        query_paths = tuple(_normalized_path(path) for path in query.exact_paths)
        hits: list[ChannelHit] = []
        for document, paths in zip(self.documents, document_paths, strict=True):
            score = max(
                (
                    _path_pair_score(
                        query_path,
                        document_path,
                        full_df=full_df,
                        suffix_df=suffix_df,
                        basename_df=basename_df,
                    )
                    for query_path in query_paths
                    for document_path in paths
                ),
                default=0.0,
            )
            if score > 0.0:
                hits.append(ChannelHit(document.revision.decision_id, score))
        hits.sort(key=lambda item: (-item.score, item.decision_id))
        return tuple(hits[:depth])


def _freeze_vectors(
    value: object, *, count: int, dimension: int
) -> tuple[tuple[float, ...], ...] | None:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        return None
    vectors: list[tuple[float, ...]] = []
    for vector in value:
        if not isinstance(vector, (list, tuple)):
            return None
        frozen = tuple(vector)
        if not _valid_vector(frozen, dimension):
            return None
        vectors.append(frozen)
    return tuple(vectors)


def _valid_vector(vector: object, dimension: int) -> bool:
    if not isinstance(vector, tuple) or len(vector) != dimension or any(
        not isinstance(value, float) or not math.isfinite(value) for value in vector
    ):
        return False
    norm = math.sqrt(sum(value * value for value in vector))
    return math.isfinite(norm) and abs(norm - 1.0) <= 1e-4


def _normalized_path(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


def _path_pair_score(
    query_path: str,
    document_path: str,
    *,
    full_df: Counter[str],
    suffix_df: Counter[str],
    basename_df: Counter[str],
) -> float:
    if query_path == document_path:
        return 3.0 + 1.0 / full_df[document_path]
    query_parts = query_path.split("/")
    document_parts = document_path.split("/")
    common = 0
    for query_part, document_part in zip(
        reversed(query_parts), reversed(document_parts), strict=False
    ):
        if query_part != document_part:
            break
        common += 1
    if common >= 2:
        suffix = "/".join(document_parts[-common:])
        return 2.0 + 1.0 / suffix_df[suffix]
    basename = document_parts[-1]
    if common == 1 and basename != "index.tsx":
        return 1.0 / basename_df[basename]
    return 0.0
