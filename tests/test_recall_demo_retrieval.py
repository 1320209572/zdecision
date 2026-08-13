"""Deterministic three-channel retrieval contracts for the recall demo."""

from __future__ import annotations

import json
import math
import unittest
from dataclasses import replace
from pathlib import Path

from zdecision.recall.session import RecallIntent
from zdecision.registry.models import DecisionRevision

from zdecision.recall.demo.bundle import VerifiedDemoBundle
from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.index import DemoIndex, DemoIndexError
from zdecision.recall.demo.projection import ProjectedQuery, project_query
from zdecision.recall.demo.retrieval import DemoRetrievalError, HybridDemoRetriever
from zdecision.recall.demo.runtime import ModelRuntimeBundle


ROOT = Path(__file__).parents[1]
PRODUCT_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
PROFILE_PATH = ROOT / "src/zdecision/recall/demo/demo-profile.json"
A = "dec_aac76c0a67bc535766c741f80066c706"
B = "dec_85e57f21d3a72fddb86749ccee0f8cbf"
C = "dec_d62aad7c1b160beaf4e31fa1a387d7e3"
D = "dec_eb09134e716c719c1df5b86e399cc2be"

PATH_A = "packages/products/third-party-services/apps/security-services/src/api/security-instance-actions.ts"
PATH_C = "packages/products/third-party-services/apps/security-services/src/app/SecurityServicesEmbedded.tsx"

DOCUMENT_B = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全能力开通部署的应用包版本选择 "
    "[正式决策] 应用包版本选项必须同时呈现版本号和 CPU 架构，以区分 ARM64 与 AMD64 应用包。 "
    "[后续实施] 设计或修改应用包版本选择时，保留用户可识别的架构信息。 "
    "[失效条件] 应用包不再区分 CPU 架构；系统能够自动确定架构且用户不再需要选择应用包 [代码范围] "
)
DOCUMENT_A = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全服务实例清理权限与动作 "
    "[正式决策] 后端授权 cleanupOwnResources 或 cleanupAnyResources 时，用户应获得对应的实例清理能力。 "
    "[后续实施] 处理安全实例可用操作时，不得隐藏后端已授权的清理入口。 "
    "[失效条件] 产品取消实例清理能力；后端动作授权模型不再用于决定用户可执行的清理操作 "
    "[代码范围] api/security-capabilities.ts；api/security-instance-actions.ts"
)
DOCUMENT_C = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全服务应用包查询能力边界 "
    "[正式决策] 安全服务中的应用包页面仅提供查询能力，不提供上传应用包能力。 "
    "[后续实施] 保持应用包页面只读，不得重新加入上传入口或上传流程。 "
    "[失效条件] 产品明确决定由安全服务重新承担应用包上传和维护职责 "
    "[代码范围] api/security-artifacts.ts；app/SecurityServicesEmbedded.tsx"
)
DOCUMENT_D = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全服务清理任务失败重试 "
    "[正式决策] 清理任务失败后的重试属于重试清理任务，不属于重新部署。 "
    "[后续实施] 清理失败场景必须保持清理语义，不得复用会让用户理解为重新部署的操作名称或流程。 "
    "[失效条件] 清理失败不再支持重试；清理重试被产品明确并入重新部署流程 "
    "[代码范围] domain/security-services/task-flow-definitions.ts"
)
EXPECTED_DOCUMENT_BATCH = (DOCUMENT_B, DOCUMENT_A, DOCUMENT_C, DOCUMENT_D)

RERANKER_B = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全能力开通部署的应用包版本选择 "
    "[正式决策] 应用包版本选项必须同时呈现版本号和 CPU 架构，以区分 ARM64 与 AMD64 应用包。 "
    "[实施约束] 设计或修改应用包版本选择时，保留用户可识别的架构信息。 "
    "[失效条件] 应用包不再区分 CPU 架构；系统能够自动确定架构且用户不再需要选择应用包 [代码范围] "
)
RERANKER_A = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全服务实例清理权限与动作 "
    "[正式决策] 后端授权 cleanupOwnResources 或 cleanupAnyResources 时，用户应获得对应的实例清理能力。 "
    "[实施约束] 处理安全实例可用操作时，不得隐藏后端已授权的清理入口。 "
    "[失效条件] 产品取消实例清理能力；后端动作授权模型不再用于决定用户可执行的清理操作 "
    "[代码范围] api/security-capabilities.ts；api/security-instance-actions.ts"
)
RERANKER_C = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全服务应用包查询能力边界 "
    "[正式决策] 安全服务中的应用包页面仅提供查询能力，不提供上传应用包能力。 "
    "[实施约束] 保持应用包页面只读，不得重新加入上传入口或上传流程。 "
    "[失效条件] 产品明确决定由安全服务重新承担应用包上传和维护职责 "
    "[代码范围] api/security-artifacts.ts；app/SecurityServicesEmbedded.tsx"
)
RERANKER_D = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全服务清理任务失败重试 "
    "[正式决策] 清理任务失败后的重试属于重试清理任务，不属于重新部署。 "
    "[实施约束] 清理失败场景必须保持清理语义，不得复用会让用户理解为重新部署的操作名称或流程。 "
    "[失效条件] 清理失败不再支持重试；清理重试被产品明确并入重新部署流程 "
    "[代码范围] domain/security-services/task-flow-definitions.ts"
)

EXPECTED_QUERY = (
    "[开发目标] 在安全服务实例列表中展示后端已授权的实例清理入口 "
    "[领域对象] SecurityServiceInstance；cleanupOwnResources；cleanupAnyResources "
    f"[相关路径] {PATH_A} "
    "[约束] 必须遵循后端动作授权结果 [排除] 不修改清理失败重试流程"
)

VECTOR_A = (1.0,) + (0.0,) * 383
VECTOR_B = (0.0, 1.0) + (0.0,) * 382
VECTOR_C = (0.0, 0.0, 1.0) + (0.0,) * 381
VECTOR_D = (0.0, 0.0, 0.0, 1.0) + (0.0,) * 380
VECTORS_BY_DOCUMENT = {
    DOCUMENT_A: VECTOR_A,
    DOCUMENT_B: VECTOR_B,
    DOCUMENT_C: VECTOR_C,
    DOCUMENT_D: VECTOR_D,
}


def _profile() -> DemoRetrievalProfile:
    return DemoRetrievalProfile.from_dict(
        json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    )


def _revision(decision_id: str) -> DecisionRevision:
    path = (
        ROOT
        / "decision-registry/products"
        / PRODUCT_ID
        / "decisions"
        / decision_id
        / "r0001.json"
    )
    return DecisionRevision.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _bundle(
    *,
    profile: DemoRetrievalProfile | None = None,
    decisions: tuple[DecisionRevision, ...] | None = None,
    manifest_digest: str = "1" * 64,
) -> VerifiedDemoBundle:
    return VerifiedDemoBundle(
        decision_space_id=PRODUCT_ID,
        product_name="third-party-services",
        repository="zstack-ui-next",
        profile=profile or _profile(),
        decisions=decisions or (_revision(A), _revision(B), _revision(C), _revision(D)),
        manifest_digest=manifest_digest,
    )


class RecordingEmbedding:
    dimension = 384

    def __init__(self, *, query_vector: tuple[float, ...] = VECTOR_B) -> None:
        self.query_vector = query_vector
        self.document_calls: list[tuple[str, ...]] = []
        self.query_calls: list[str] = []

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.document_calls.append(texts)
        return tuple(VECTORS_BY_DOCUMENT[text] for text in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        self.query_calls.append(text)
        return self.query_vector


class RecordingReranker:
    def __init__(self, scores: tuple[float, ...] = ()) -> None:
        self.scores = scores
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def score(self, query: str, documents: tuple[str, ...]) -> tuple[float, ...]:
        self.calls.append((query, documents))
        return self.scores[: len(documents)]


def _runtime(
    bundle: VerifiedDemoBundle,
    *,
    embedding: RecordingEmbedding | None = None,
    reranker: RecordingReranker | None = None,
) -> ModelRuntimeBundle:
    return ModelRuntimeBundle(
        profile_digest=bundle.profile.digest,
        embedding=embedding or RecordingEmbedding(),
        reranker=reranker or RecordingReranker(),
    )


def _cleanup_intent() -> RecallIntent:
    return RecallIntent(
        target_decision_space_ids=(PRODUCT_ID,),
        explicit_multi_space=False,
        feature_goal="在安全服务实例列表中展示后端已授权的实例清理入口",
        domain_objects=(
            "SecurityServiceInstance",
            "cleanupOwnResources",
            "cleanupAnyResources",
        ),
        repository_relative_paths=(PATH_A,),
        constraints=("必须遵循后端动作授权结果",),
        exclusions=("不修改清理失败重试流程",),
    )


class DemoIndexTests(unittest.TestCase):
    def test_build_sanitizes_structurally_invalid_embedding_batches(self) -> None:
        """None, generators, and malformed nesting must not leak runtime type errors."""
        bundle = _bundle()
        factories = {
            "none": lambda: None,
            "generator": lambda: (vector for vector in EXPECTED_DOCUMENT_BATCH),
            "malformed_nested": lambda: (1.0, VECTOR_A, VECTOR_C, VECTOR_D),
        }

        class StructuralEmbedding(RecordingEmbedding):
            def __init__(self, output: object) -> None:
                super().__init__()
                self.output = output

            def embed_documents(self, texts: tuple[str, ...]):
                self.document_calls.append(texts)
                return self.output

        for label, factory in factories.items():
            with self.subTest(label=label):
                embedding = StructuralEmbedding(factory())
                with self.assertRaises(DemoIndexError) as captured:
                    DemoIndex.build(
                        bundle, _runtime(bundle, embedding=embedding)
                    )
                self.assertEqual("embedding_output_invalid", captured.exception.code)
                self.assertEqual("embedding_output_invalid", str(captured.exception))

    def test_build_deep_freezes_mutable_adapter_vectors(self) -> None:
        """Mutating adapter-owned vector lists after build must not alter the index."""
        bundle = _bundle()

        class MutableEmbedding(RecordingEmbedding):
            def __init__(self) -> None:
                super().__init__()
                self.returned: list[list[float]] = []

            def embed_documents(self, texts: tuple[str, ...]) -> list[list[float]]:
                self.document_calls.append(texts)
                self.returned = [list(VECTORS_BY_DOCUMENT[text]) for text in texts]
                return self.returned

        embedding = MutableEmbedding()
        index = DemoIndex.build(bundle, _runtime(bundle, embedding=embedding))
        self.assertEqual(B, index.dense_search(VECTOR_B, 4)[0].decision_id)

        embedding.returned[0][:] = VECTOR_A
        embedding.returned[1][:] = VECTOR_B

        self.assertEqual(B, index.dense_search(VECTOR_B, 4)[0].decision_id)
        self.assertTrue(all(isinstance(vector, tuple) for vector in index.vectors))

    def test_build_embeds_one_exact_sorted_projection_batch(self) -> None:
        """Changing projection order or batching documents separately must fail."""
        bundle = _bundle()
        embedding = RecordingEmbedding()

        index = DemoIndex.build(bundle, _runtime(bundle, embedding=embedding))

        self.assertEqual([EXPECTED_DOCUMENT_BATCH], embedding.document_calls)
        self.assertEqual(
            (B, A, C, D),
            tuple(item.revision.decision_id for item in index.documents),
        )
        self.assertEqual((VECTOR_B, VECTOR_A, VECTOR_C, VECTOR_D), index.vectors)
        self.assertEqual(bundle.manifest_digest, index.manifest_digest)

    def test_build_rejects_foreign_or_inactive_leaf_before_embedding(self) -> None:
        """A non-target or inactive formal leaf must never reach the model runtime."""
        for mutation in ("foreign", "inactive"):
            with self.subTest(mutation=mutation):
                bundle = _bundle()
                decisions = list(bundle.decisions)
                if mutation == "foreign":
                    decisions[0] = replace(decisions[0], repositories=("other-repo",))
                else:
                    inactive = object.__new__(DecisionRevision)
                    for name, value in decisions[0].__dict__.items():
                        object.__setattr__(inactive, name, value)
                    object.__setattr__(inactive, "lifecycle", "inactive")
                    decisions[0] = inactive
                changed = replace(bundle, decisions=tuple(decisions))
                embedding = RecordingEmbedding()

                with self.assertRaises(DemoIndexError):
                    DemoIndex.build(changed, _runtime(changed, embedding=embedding))

                self.assertEqual([], embedding.document_calls)

    def test_build_rejects_duplicate_or_conflicting_identity_before_embedding(self) -> None:
        """One identity appearing twice, with equal or conflicting bytes, must fail closed."""
        original = _revision(A)
        conflicting = replace(original, claim="冲突的正式决策内容")
        for duplicate in (original, conflicting):
            with self.subTest(conflicting=duplicate is conflicting):
                bundle = _bundle(decisions=(original, duplicate))
                embedding = RecordingEmbedding()
                with self.assertRaises(DemoIndexError):
                    DemoIndex.build(bundle, _runtime(bundle, embedding=embedding))
                self.assertEqual([], embedding.document_calls)

    def test_build_rejects_wrong_count_dimension_finiteness_or_norm(self) -> None:
        """Malformed model output must not produce a partially usable index."""
        bundle = _bundle()

        class MalformedEmbedding(RecordingEmbedding):
            def __init__(self, replacement: tuple[tuple[float, ...], ...]) -> None:
                super().__init__()
                self.replacement = replacement

            def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
                self.document_calls.append(texts)
                return self.replacement

        invalid_outputs = (
            (VECTOR_B, VECTOR_A, VECTOR_C),
            ((1.0,), VECTOR_A, VECTOR_C, VECTOR_D),
            (((math.nan,) + (0.0,) * 383), VECTOR_A, VECTOR_C, VECTOR_D),
            (((0.5,) + (0.0,) * 383), VECTOR_A, VECTOR_C, VECTOR_D),
            ((("not-a-number",) + (0.0,) * 383), VECTOR_A, VECTOR_C, VECTOR_D),
        )
        for output in invalid_outputs:
            with self.subTest(first_length=len(output[0])):
                with self.assertRaises(DemoIndexError):
                    DemoIndex.build(bundle, _runtime(bundle, embedding=MalformedEmbedding(output)))

    def test_three_channels_rank_independently(self) -> None:
        """Lexical, cosine, and exact-path evidence must each select its own document."""
        bundle = _bundle()
        index = DemoIndex.build(bundle, _runtime(bundle))
        query = ProjectedQuery(
            embedding_text="unused",
            reranker_text="unused",
            lexical_tokens=("cleanupownresources",),
            exact_paths=(PATH_C,),
        )

        self.assertEqual(A, index.bm25_search(query, 4)[0].decision_id)
        self.assertEqual(B, index.dense_search(VECTOR_B, 4)[0].decision_id)
        self.assertEqual(C, index.path_search(query, 4)[0].decision_id)

    def test_exact_full_path_outranks_shared_generic_basename(self) -> None:
        """A generic index.tsx basename must not compete with an exact full path."""
        bundle = _bundle()
        index = DemoIndex.build(bundle, _runtime(bundle))
        query = ProjectedQuery(
            embedding_text="unused",
            reranker_text="unused",
            lexical_tokens=(),
            exact_paths=(PATH_C, "index.tsx"),
        )

        hits = index.path_search(query, 4)

        self.assertEqual((C,), tuple(hit.decision_id for hit in hits))
        self.assertEqual(4.0, hits[0].score)

    def test_shared_full_path_ties_and_does_not_imply_unique_ownership(self) -> None:
        """A colliding exact path must retain every owner at the same DF score."""
        all_ids = tuple(
            path.parent.name
            for path in sorted(
                (ROOT / "decision-registry/products" / PRODUCT_ID / "decisions").glob(
                    "*/r0001.json"
                )
            )
        )
        bundle = _bundle(decisions=tuple(_revision(decision_id) for decision_id in all_ids))

        class UniformEmbedding(RecordingEmbedding):
            def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
                self.document_calls.append(texts)
                return (VECTOR_A,) * len(texts)

        index = DemoIndex.build(
            bundle, _runtime(bundle, embedding=UniformEmbedding())
        )
        shared_path = (
            "packages/products/third-party-services/apps/security-services/src/api/"
            "security-capabilities.ts"
        )
        query = ProjectedQuery("unused", "unused", (), (shared_path,))

        hits = index.path_search(query, 10)

        self.assertEqual(
            ("dec_48162f34e83238a921b65a82dd77dd9c", A),
            tuple(hit.decision_id for hit in hits),
        )
        self.assertEqual((3.5, 3.5), tuple(hit.score for hit in hits))

    def test_exclusions_do_not_become_positive_bm25_terms(self) -> None:
        """An excluded cleanup literal must not make cleanup Decisions lexical hits."""
        bundle = _bundle()
        index = DemoIndex.build(bundle, _runtime(bundle))
        intent = RecallIntent(
            target_decision_space_ids=(PRODUCT_ID,),
            explicit_multi_space=False,
            feature_goal="quasar",
            domain_objects=(),
            repository_relative_paths=(),
            constraints=(),
            exclusions=("cleanupOwnResources 清理失败重试流程",),
        )

        self.assertEqual((), index.bm25_search(project_query(intent), 4))

    def test_bm25_gives_no_query_term_duplication_benefit(self) -> None:
        """Repeating one query token must not multiply the document score."""
        bundle = _bundle()
        index = DemoIndex.build(bundle, _runtime(bundle))
        single = ProjectedQuery("unused", "unused", ("cleanupownresources",), ())
        repeated = ProjectedQuery(
            "unused",
            "unused",
            ("cleanupownresources", "cleanupownresources"),
            (),
        )

        self.assertEqual(index.bm25_search(single, 4), index.bm25_search(repeated, 4))


class HybridDemoRetrieverTests(unittest.TestCase):
    def test_sanitizes_structurally_invalid_query_embeddings_without_fallback(self) -> None:
        """Query output shape failures must stop before reranking without leaking types."""
        factories = {
            "none": lambda: None,
            "generator": lambda: (value for value in VECTOR_B),
            "malformed_nested": lambda: (VECTOR_B,),
        }

        class StructuralEmbedding(RecordingEmbedding):
            def __init__(self, output: object) -> None:
                super().__init__()
                self.output = output

            def embed_query(self, text: str):
                self.query_calls.append(text)
                return self.output

        for label, factory in factories.items():
            with self.subTest(label=label):
                bundle = _bundle()
                embedding = StructuralEmbedding(factory())
                reranker = RecordingReranker((1.0,) * 4)
                runtime = _runtime(
                    bundle, embedding=embedding, reranker=reranker
                )
                index = DemoIndex.build(bundle, runtime)
                with self.assertRaises(DemoRetrievalError) as captured:
                    HybridDemoRetriever().retrieve(
                        _cleanup_intent(), bundle, index, runtime
                    )
                self.assertEqual("query_embedding_invalid", captured.exception.code)
                self.assertEqual("query_embedding_invalid", str(captured.exception))
                self.assertEqual([], reranker.calls)

    def test_sanitizes_structurally_invalid_reranker_batches_without_fallback(self) -> None:
        """Reranker output shape failures must not leak type errors or return items."""
        factories = {
            "none": lambda: None,
            "generator": lambda: (score for score in (1.0,) * 4),
            "malformed_nested": lambda: ((1.0,),) * 4,
        }

        class StructuralReranker(RecordingReranker):
            def __init__(self, output: object) -> None:
                super().__init__()
                self.output = output

            def score(self, query: str, documents: tuple[str, ...]):
                self.calls.append((query, documents))
                return self.output

        for label, factory in factories.items():
            with self.subTest(label=label):
                bundle = _bundle()
                embedding = RecordingEmbedding()
                reranker = StructuralReranker(factory())
                runtime = _runtime(
                    bundle, embedding=embedding, reranker=reranker
                )
                index = DemoIndex.build(bundle, runtime)
                with self.assertRaises(DemoRetrievalError) as captured:
                    HybridDemoRetriever().retrieve(
                        _cleanup_intent(), bundle, index, runtime
                    )
                self.assertEqual("reranker_output_invalid", captured.exception.code)
                self.assertEqual("reranker_output_invalid", str(captured.exception))
                self.assertEqual(1, len(embedding.query_calls))

    def test_rejects_every_projected_field_tamper_before_query_runtime(self) -> None:
        """Any searchable projection change must fail before query or reranker calls."""
        bundle = _bundle()
        embedding = RecordingEmbedding()
        reranker = RecordingReranker((1.0,) * 4)
        runtime = _runtime(bundle, embedding=embedding, reranker=reranker)
        index = DemoIndex.build(bundle, runtime)
        embedding.document_calls.clear()
        original = index.documents[0]
        mutations = {
            "embedding_text": replace(
                original, embedding_text=original.embedding_text + " attacker"
            ),
            "reranker_text": replace(
                original, reranker_text=original.reranker_text + " attacker"
            ),
            "lexical_tokens": replace(
                original, lexical_tokens=original.lexical_tokens + ("attacker",)
            ),
            "exact_paths": replace(
                original, exact_paths=original.exact_paths + ("attacker/path.ts",)
            ),
        }

        for field_name, tampered_document in mutations.items():
            with self.subTest(field_name=field_name):
                tampered_index = replace(
                    index,
                    documents=(tampered_document, *index.documents[1:]),
                )
                with self.assertRaises(DemoRetrievalError) as captured:
                    HybridDemoRetriever().retrieve(
                        _cleanup_intent(), bundle, tampered_index, runtime
                    )
                self.assertEqual("decision_identity_conflict", captured.exception.code)
                self.assertEqual("decision_identity_conflict", str(captured.exception))

        self.assertEqual([], embedding.query_calls)
        self.assertEqual([], reranker.calls)

    def test_validates_leaf_manifest_profile_and_runtime_before_query_embedding(self) -> None:
        """Any cross-generation or wrong-leaf binding must stop before model calls."""
        bundle = _bundle()
        embedding = RecordingEmbedding()
        reranker = RecordingReranker((1.0,) * 4)
        runtime = _runtime(bundle, embedding=embedding, reranker=reranker)
        index = DemoIndex.build(bundle, runtime)
        embedding.document_calls.clear()
        invalid_cases = (
            (
                RecallIntent(
                    target_decision_space_ids=("prod_00000000000000000000000000000000",),
                    explicit_multi_space=False,
                    feature_goal="cleanupOwnResources",
                    domain_objects=(),
                    repository_relative_paths=(),
                    constraints=(),
                    exclusions=(),
                ),
                bundle,
                index,
                runtime,
            ),
            (_cleanup_intent(), replace(bundle, manifest_digest="2" * 64), index, runtime),
            (
                _cleanup_intent(),
                bundle,
                index,
                replace(runtime, profile_digest="3" * 64),
            ),
        )
        for intent, candidate_bundle, candidate_index, candidate_runtime in invalid_cases:
            with self.subTest(
                intent=intent.feature_goal,
                manifest=candidate_bundle.manifest_digest,
            ):
                with self.assertRaises(DemoRetrievalError):
                    HybridDemoRetriever().retrieve(
                        intent, candidate_bundle, candidate_index, candidate_runtime
                    )
        self.assertEqual([], embedding.query_calls)
        self.assertEqual([], reranker.calls)

    def test_happy_path_embeds_query_once_and_ranks_literal_cleanup_decision_first(self) -> None:
        """Dropping either cleanup literals or the unique path must change rank one."""
        bundle = _bundle()
        embedding = RecordingEmbedding(query_vector=VECTOR_B)
        reranker = RecordingReranker((3.5, 3.5, 3.5, 3.5))
        runtime = _runtime(bundle, embedding=embedding, reranker=reranker)
        index = DemoIndex.build(bundle, runtime)

        result = HybridDemoRetriever().retrieve(
            _cleanup_intent(), bundle, index, runtime
        )

        self.assertEqual([EXPECTED_QUERY], embedding.query_calls)
        self.assertEqual(
            [(EXPECTED_QUERY, (RERANKER_A, RERANKER_B, RERANKER_D, RERANKER_C))],
            reranker.calls,
        )
        self.assertEqual(A, result.items[0].revision.decision_id)
        self.assertEqual("semantic+lexical+path", result.items[0].match_reason)
        self.assertEqual(_cleanup_intent().digest, result.intent_digest)
        self.assertEqual(bundle.profile.digest, result.profile_digest)
        self.assertEqual(bundle.manifest_digest, result.manifest_digest)
        serialized = result.to_dict()
        self.assertEqual(A, serialized["items"][0]["decision"]["decision_id"])
        self.assertEqual(_revision(A).to_dict(), serialized["items"][0]["decision"])

    def test_positional_reranker_scores_reorder_and_threshold_candidates(self) -> None:
        """Scores must remain attached to caller positions, including a negative drop."""
        bundle = _bundle()
        reranker = RecordingReranker((3.1, 3.9, 3.8, 2.9))
        runtime = _runtime(bundle, reranker=reranker)
        index = DemoIndex.build(bundle, runtime)

        result = HybridDemoRetriever().retrieve(
            _cleanup_intent(), bundle, index, runtime
        )

        self.assertEqual(
            (B, D, A), tuple(item.revision.decision_id for item in result.items)
        )
        self.assertEqual((3.9, 3.8, 3.1), tuple(item.reranker_score for item in result.items))

    def test_weighted_rrf_uses_signed_depths_weights_and_identity_tie_break(self) -> None:
        """Changing signed depth, weight, or identity tie order must fail this fusion."""
        profile = replace(
            _profile(),
            bm25_depth=1,
            dense_depth=1,
            path_depth=1,
            union_depth=2,
            rerank_depth=2,
            bm25_weight=1.0,
            dense_weight=1.0,
            path_weight=0.0,
        )
        bundle = _bundle(profile=profile)
        reranker = RecordingReranker((3.5, 3.5))
        runtime = _runtime(bundle, reranker=reranker)
        index = DemoIndex.build(bundle, runtime)
        intent = RecallIntent(
            target_decision_space_ids=(PRODUCT_ID,),
            explicit_multi_space=False,
            feature_goal="cleanupOwnResources",
            domain_objects=(),
            repository_relative_paths=(),
            constraints=(),
            exclusions=(),
        )

        result = HybridDemoRetriever().retrieve(intent, bundle, index, runtime)

        self.assertEqual((B, A), tuple(item.revision.decision_id for item in result.items))
        self.assertEqual(
            ("semantic", "lexical"),
            tuple(item.match_reason for item in result.items),
        )
        self.assertTrue(
            all(math.isclose(item.fused_score, 1.0 / 61.0) for item in result.items)
        )
        self.assertEqual(2, len(reranker.calls[0][1]))

    def test_signed_threshold_filters_below_and_keeps_boundary_without_fallback(self) -> None:
        """The real signed 3.0 threshold must filter 2.999 and retain 3.0."""
        cases = (
            (
                "boundary-retained",
                (2.999, 3.0, 2.999, 2.999),
                ((B, 3.0),),
            ),
            ("all-below-abstains", (2.999,) * 4, ()),
        )
        for label, scores, expected in cases:
            with self.subTest(label=label):
                bundle = _bundle()
                reranker = RecordingReranker(scores)
                runtime = _runtime(bundle, reranker=reranker)
                index = DemoIndex.build(bundle, runtime)

                result = HybridDemoRetriever().retrieve(
                    _cleanup_intent(), bundle, index, runtime
                )

                self.assertEqual(
                    expected,
                    tuple(
                        (item.revision.decision_id, item.reranker_score)
                        for item in result.items
                    ),
                )
                self.assertEqual(1, len(reranker.calls))

    def test_malformed_query_or_reranker_output_fails_without_fallback(self) -> None:
        """Invalid local model output must never degrade into lexical-only retrieval."""
        for query_vector, scores in (
            ((1.0,), (1.0,) * 4),
            (VECTOR_B, (1.0,)),
            (VECTOR_B, (math.nan,) * 4),
        ):
            with self.subTest(query_length=len(query_vector), scores=scores):
                bundle = _bundle()
                runtime = _runtime(
                    bundle,
                    embedding=RecordingEmbedding(query_vector=query_vector),
                    reranker=RecordingReranker(scores),
                )
                index = DemoIndex.build(bundle, runtime)
                with self.assertRaises(DemoRetrievalError):
                    HybridDemoRetriever().retrieve(
                        _cleanup_intent(), bundle, index, runtime
                    )

    def test_output_obeys_item_and_complete_canonical_byte_budgets(self) -> None:
        """Packing must stop at both signed limits without truncating a Decision."""
        all_ids = tuple(
            path.parent.name
            for path in sorted(
                (ROOT / "decision-registry/products" / PRODUCT_ID / "decisions").glob(
                    "*/r0001.json"
                )
            )
        )
        bundle = _bundle(decisions=tuple(_revision(decision_id) for decision_id in all_ids))

        class UniformEmbedding(RecordingEmbedding):
            def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
                self.document_calls.append(texts)
                return (VECTOR_A,) * len(texts)

        embedding = UniformEmbedding(query_vector=VECTOR_A)
        reranker = RecordingReranker((3.5,) * 10)
        runtime = _runtime(bundle, embedding=embedding, reranker=reranker)
        index = DemoIndex.build(bundle, runtime)
        intent = RecallIntent(
            target_decision_space_ids=(PRODUCT_ID,),
            explicit_multi_space=False,
            feature_goal="quasar-nebula",
            domain_objects=(),
            repository_relative_paths=(),
            constraints=(),
            exclusions=(),
        )

        result = HybridDemoRetriever().retrieve(intent, bundle, index, runtime)

        self.assertLessEqual(len(result.items), 8)
        self.assertLessEqual(
            sum(
                len(
                    (
                        ROOT
                        / "decision-registry/products"
                        / PRODUCT_ID
                        / "decisions"
                        / item.revision.decision_id
                        / "r0001.json"
                    ).read_bytes()
                )
                for item in result.items
            ),
            10_000,
        )
        self.assertTrue(
            all(
                (
                    ROOT
                    / "decision-registry/products"
                    / PRODUCT_ID
                    / "decisions"
                    / item.revision.decision_id
                    / "r0001.json"
                ).read_bytes()
                == (
                    json.dumps(
                        item.revision.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
                for item in result.items
            )
        )

    def test_oversized_first_item_is_skipped_not_truncated(self) -> None:
        """An item larger than the total budget must not hide a later complete item."""
        oversized = replace(_revision(A), claim="quasar" * 2_000)
        retained = _revision(B)
        bundle = _bundle(decisions=(oversized, retained))

        class UniformEmbedding(RecordingEmbedding):
            def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
                self.document_calls.append(texts)
                return (VECTOR_A,) * len(texts)

        runtime = _runtime(
            bundle,
            embedding=UniformEmbedding(query_vector=VECTOR_A),
            reranker=RecordingReranker((3.5, 3.5)),
        )
        index = DemoIndex.build(bundle, runtime)
        intent = RecallIntent(
            target_decision_space_ids=(PRODUCT_ID,),
            explicit_multi_space=False,
            feature_goal="quasar",
            domain_objects=(),
            repository_relative_paths=(),
            constraints=(),
            exclusions=(),
        )

        result = HybridDemoRetriever().retrieve(intent, bundle, index, runtime)

        self.assertEqual((B,), tuple(item.revision.decision_id for item in result.items))
        self.assertEqual(retained.to_dict(), result.to_dict()["items"][0]["decision"])

    def test_match_reasons_are_from_closed_deterministic_set(self) -> None:
        """Channel evidence must never generate prose or reordered reason labels."""
        bundle = _bundle()
        runtime = _runtime(bundle, reranker=RecordingReranker((3.5,) * 4))
        index = DemoIndex.build(bundle, runtime)
        result = HybridDemoRetriever().retrieve(
            _cleanup_intent(), bundle, index, runtime
        )
        allowed = {
            "semantic",
            "lexical",
            "path",
            "semantic+lexical",
            "semantic+path",
            "lexical+path",
            "semantic+lexical+path",
        }
        self.assertTrue(result.items)
        self.assertTrue(all(item.match_reason in allowed for item in result.items))


if __name__ == "__main__":
    unittest.main()
