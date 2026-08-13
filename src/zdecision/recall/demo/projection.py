"""Deterministic, runtime-neutral projections for the recall demo."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.session import RecallIntent
from zdecision.registry.models import DecisionRevision


@dataclass(frozen=True)
class ProjectedDecision:
    revision: DecisionRevision
    canonical_bytes: bytes
    embedding_text: str
    reranker_text: str
    lexical_tokens: tuple[str, ...]
    exact_paths: tuple[str, ...]


@dataclass(frozen=True)
class ProjectedQuery:
    embedding_text: str
    reranker_text: str
    lexical_tokens: tuple[str, ...]
    exact_paths: tuple[str, ...]


def tokenize(text: str) -> tuple[str, ...]:
    """Tokenize normalized Unicode into CJK unigrams/bigrams and word runs."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    run: list[str] = []
    run_is_cjk: bool | None = None

    def flush() -> None:
        nonlocal run, run_is_cjk
        if not run:
            return
        if run_is_cjk:
            tokens.extend(run)
            tokens.extend(run[index] + run[index + 1] for index in range(len(run) - 1))
        else:
            tokens.append("".join(run))
        run = []
        run_is_cjk = None

    for character in normalized:
        cjk = _is_cjk(character)
        category = unicodedata.category(character)
        word = cjk or category[0] in {"L", "M", "N"} or category == "Pc"
        if not word:
            flush()
            continue
        if run_is_cjk is not None and cjk != run_is_cjk:
            flush()
        run_is_cjk = cjk
        run.append(character)
    flush()
    return tuple(tokens)


def project_decision(revision: DecisionRevision) -> ProjectedDecision:
    semantic_paths = tuple(
        path
        for path in (_semantic_path(path) for path in revision.paths)
        if path is not None
    )
    shared = (
        f"[产品] {revision.product_name} "
        f"[仓库] {'；'.join(revision.repositories)} "
        f"[主题] {revision.scope_summary} "
        f"[正式决策] {revision.claim} "
    )
    ending = (
        f"[失效条件] {'；'.join(revision.invalidation_conditions)} "
        f"[代码范围] {'；'.join(semantic_paths)}"
    )
    embedding_text = f"{shared}[后续实施] {revision.future_action} {ending}"
    reranker_text = f"{shared}[实施约束] {revision.future_action} {ending}"
    return ProjectedDecision(
        revision=revision,
        canonical_bytes=canonical_json_bytes(revision.to_dict()),
        embedding_text=embedding_text,
        reranker_text=reranker_text,
        lexical_tokens=tokenize(embedding_text),
        exact_paths=revision.paths,
    )


def project_query(intent: RecallIntent) -> ProjectedQuery:
    text = (
        f"[开发目标] {intent.feature_goal} "
        f"[领域对象] {'；'.join(intent.domain_objects)} "
        f"[相关路径] {'；'.join(intent.repository_relative_paths)} "
        f"[约束] {'；'.join(intent.constraints)} "
        f"[排除] {'；'.join(intent.exclusions)}"
    )
    positive_text = " ".join(
        (
            intent.feature_goal,
            *intent.domain_objects,
            *intent.repository_relative_paths,
            *intent.constraints,
        )
    )
    lexical_tokens = tuple(dict.fromkeys(tokenize(positive_text)))
    return ProjectedQuery(
        embedding_text=text,
        reranker_text=text,
        lexical_tokens=lexical_tokens,
        exact_paths=intent.repository_relative_paths,
    )


def _is_cjk(character: str) -> bool:
    value = ord(character)
    return (
        0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x2EBEF
        or 0x30000 <= value <= 0x3134F
    )


def _semantic_path(path: str) -> str | None:
    parts = path.split("/")
    if parts[-1].casefold() == "index.tsx":
        return None
    try:
        source_index = parts.index("src")
    except ValueError:
        return path
    return "/".join(parts[source_index + 1 :])
