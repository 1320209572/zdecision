"""Strict immutable retrieval profile for the third-party-services demo."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from zdecision.jsonio import canonical_json_bytes


_PROFILE_ID = "recall-demo-third-party-services-v1"
_DECISION_SPACE_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
_PRODUCT_NAME = "third-party-services"
_REPOSITORY = "zstack-ui-next"
_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
_EMBEDDING_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
_RERANKER_MODEL = "BAAI/bge-reranker-base"
_RERANKER_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
_REVISION_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODEL_FILE_NAMES = frozenset(
    (
        "config.json",
        "model.safetensors",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    )
)

_PROFILE_FIELDS = frozenset(
    (
        "schema_version",
        "profile_id",
        "decision_space_id",
        "product_name",
        "repository",
        "embedding",
        "reranker",
        "embedding_dimension",
        "bm25_depth",
        "dense_depth",
        "path_depth",
        "union_depth",
        "rerank_depth",
        "reciprocal_rank_constant",
        "bm25_weight",
        "dense_weight",
        "path_weight",
        "reranker_threshold",
        "max_shortlist_items",
        "max_shortlist_utf8_bytes",
    )
)


def _require_fields(value: object, expected: frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise ValueError(f"{name} has invalid fields")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def _finite_number(value: object, name: str) -> float:
    if not isinstance(value, float):
        raise ValueError(f"{name} must be a finite JSON float")
    number = value
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True)
class DemoModelFileBinding:
    name: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {"sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class DemoModelSpec:
    model_id: str
    revision: str
    max_tokens: int
    files: tuple[DemoModelFileBinding, ...]

    @classmethod
    def from_dict(cls, value: object, *, model_id: str, revision: str) -> "DemoModelSpec":
        mapping = _require_fields(
            value,
            frozenset(("model_id", "revision", "max_tokens", "files")),
            "Model spec",
        )
        actual_model_id = mapping["model_id"]
        actual_revision = mapping["revision"]
        max_tokens = _integer(mapping["max_tokens"], "Model max_tokens")
        if actual_model_id != model_id or actual_revision != revision:
            raise ValueError("Model identity is invalid")
        if (
            not isinstance(actual_revision, str)
            or _REVISION_SHA.fullmatch(actual_revision) is None
        ):
            raise ValueError("Model revision is invalid")
        if max_tokens != 512:
            raise ValueError("Model max_tokens is invalid")
        file_values = _require_fields(
            mapping["files"], _MODEL_FILE_NAMES, "Model files"
        )
        files: list[DemoModelFileBinding] = []
        for name in sorted(_MODEL_FILE_NAMES):
            binding = _require_fields(
                file_values[name], frozenset(("sha256", "size")), "Model file"
            )
            size = _integer(binding["size"], "Model file size")
            digest = binding["sha256"]
            if (
                size < 0
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
            ):
                raise ValueError("Model file binding is invalid")
            files.append(
                DemoModelFileBinding(name=name, size=size, sha256=digest)
            )
        return cls(
            model_id=model_id,
            revision=revision,
            max_tokens=max_tokens,
            files=tuple(files),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "revision": self.revision,
            "max_tokens": self.max_tokens,
            "files": {binding.name: binding.to_dict() for binding in self.files},
        }


@dataclass(frozen=True)
class DemoRetrievalProfile:
    schema_version: Literal[1]
    profile_id: str
    decision_space_id: str
    product_name: str
    repository: str
    embedding: DemoModelSpec
    reranker: DemoModelSpec
    embedding_dimension: int
    bm25_depth: int
    dense_depth: int
    path_depth: int
    union_depth: int
    rerank_depth: int
    reciprocal_rank_constant: int
    bm25_weight: float
    dense_weight: float
    path_weight: float
    reranker_threshold: float
    max_shortlist_items: Literal[8]
    max_shortlist_utf8_bytes: Literal[10000]

    @classmethod
    def from_dict(cls, value: object) -> "DemoRetrievalProfile":
        mapping = _require_fields(value, _PROFILE_FIELDS, "Demo retrieval profile")
        schema_version = _integer(mapping["schema_version"], "schema_version")
        embedding_dimension = _integer(
            mapping["embedding_dimension"], "embedding_dimension"
        )
        max_shortlist_items = _integer(
            mapping["max_shortlist_items"], "max_shortlist_items"
        )
        max_shortlist_utf8_bytes = _integer(
            mapping["max_shortlist_utf8_bytes"], "max_shortlist_utf8_bytes"
        )
        if (
            schema_version != 1
            or mapping["profile_id"] != _PROFILE_ID
            or mapping["decision_space_id"] != _DECISION_SPACE_ID
            or mapping["product_name"] != _PRODUCT_NAME
            or mapping["repository"] != _REPOSITORY
            or embedding_dimension != 384
            or max_shortlist_items != 8
            or max_shortlist_utf8_bytes != 10_000
        ):
            raise ValueError("Demo retrieval profile identity is invalid")
        embedding = DemoModelSpec.from_dict(
            mapping["embedding"], model_id=_EMBEDDING_MODEL, revision=_EMBEDDING_REVISION
        )
        reranker = DemoModelSpec.from_dict(
            mapping["reranker"], model_id=_RERANKER_MODEL, revision=_RERANKER_REVISION
        )
        depths = {
            name: _integer(mapping[name], name)
            for name in ("bm25_depth", "dense_depth", "path_depth", "union_depth", "rerank_depth")
        }
        if any(depth not in range(1, 11) for depth in depths.values()):
            raise ValueError("Retrieval depth is invalid")
        if depths["rerank_depth"] > depths["union_depth"]:
            raise ValueError("Rerank depth exceeds union depth")
        rrf_constant = _integer(mapping["reciprocal_rank_constant"], "reciprocal_rank_constant")
        if rrf_constant != 60:
            raise ValueError("Reciprocal rank constant is invalid")
        weights = {
            name: _finite_number(mapping[name], name)
            for name in ("bm25_weight", "dense_weight", "path_weight")
        }
        if any(weight < 0.0 for weight in weights.values()) or not any(weights.values()):
            raise ValueError("Fusion weights are invalid")
        threshold = _finite_number(mapping["reranker_threshold"], "reranker_threshold")
        if not -20.0 <= threshold <= 20.0:
            raise ValueError("Reranker threshold is invalid")
        return cls(
            schema_version=1,
            profile_id=_PROFILE_ID,
            decision_space_id=_DECISION_SPACE_ID,
            product_name=_PRODUCT_NAME,
            repository=_REPOSITORY,
            embedding=embedding,
            reranker=reranker,
            embedding_dimension=384,
            **depths,
            reciprocal_rank_constant=rrf_constant,
            **weights,
            reranker_threshold=threshold,
            max_shortlist_items=8,
            max_shortlist_utf8_bytes=10_000,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "decision_space_id": self.decision_space_id,
            "product_name": self.product_name,
            "repository": self.repository,
            "embedding": self.embedding.to_dict(),
            "reranker": self.reranker.to_dict(),
            "embedding_dimension": self.embedding_dimension,
            "bm25_depth": self.bm25_depth,
            "dense_depth": self.dense_depth,
            "path_depth": self.path_depth,
            "union_depth": self.union_depth,
            "rerank_depth": self.rerank_depth,
            "reciprocal_rank_constant": self.reciprocal_rank_constant,
            "bm25_weight": self.bm25_weight,
            "dense_weight": self.dense_weight,
            "path_weight": self.path_weight,
            "reranker_threshold": self.reranker_threshold,
            "max_shortlist_items": self.max_shortlist_items,
            "max_shortlist_utf8_bytes": self.max_shortlist_utf8_bytes,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()
