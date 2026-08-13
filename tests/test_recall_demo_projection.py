"""Literal projection and Unicode tokenization contracts for the recall demo."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from zdecision.recall.session import RecallIntent
from zdecision.registry.models import DecisionRevision

from zdecision.recall.demo.projection import (
    project_decision,
    project_query,
    tokenize,
)


ROOT = Path(__file__).parents[1]
PRODUCT_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
CLEANUP_ID = "dec_aac76c0a67bc535766c741f80066c706"
CLEANUP_PATH = (
    ROOT
    / "decision-registry/products"
    / PRODUCT_ID
    / "decisions"
    / CLEANUP_ID
    / "r0001.json"
)

EXPECTED_EMBEDDING = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全服务实例清理权限与动作 "
    "[正式决策] 后端授权 cleanupOwnResources 或 cleanupAnyResources 时，用户应获得对应的实例清理能力。 "
    "[后续实施] 处理安全实例可用操作时，不得隐藏后端已授权的清理入口。 "
    "[失效条件] 产品取消实例清理能力；后端动作授权模型不再用于决定用户可执行的清理操作 "
    "[代码范围] api/security-capabilities.ts；api/security-instance-actions.ts"
)
EXPECTED_RERANKER = (
    "[产品] third-party-services [仓库] zstack-ui-next [主题] 安全服务实例清理权限与动作 "
    "[正式决策] 后端授权 cleanupOwnResources 或 cleanupAnyResources 时，用户应获得对应的实例清理能力。 "
    "[实施约束] 处理安全实例可用操作时，不得隐藏后端已授权的清理入口。 "
    "[失效条件] 产品取消实例清理能力；后端动作授权模型不再用于决定用户可执行的清理操作 "
    "[代码范围] api/security-capabilities.ts；api/security-instance-actions.ts"
)
EXPECTED_QUERY = (
    "[开发目标] 在安全服务实例列表中展示后端已授权的实例清理入口 "
    "[领域对象] SecurityServiceInstance；cleanupOwnResources；cleanupAnyResources "
    "[相关路径] packages/products/third-party-services/apps/security-services/src/api/security-instance-actions.ts "
    "[约束] 必须遵循后端动作授权结果 [排除] 不修改清理失败重试流程"
)


def _cleanup_revision() -> DecisionRevision:
    return DecisionRevision.from_dict(json.loads(CLEANUP_PATH.read_text(encoding="utf-8")))


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
        repository_relative_paths=(
            "packages/products/third-party-services/apps/security-services/src/api/security-instance-actions.ts",
        ),
        constraints=("必须遵循后端动作授权结果",),
        exclusions=("不修改清理失败重试流程",),
    )


class DemoProjectionTests(unittest.TestCase):
    def test_tokenize_handles_cjk_literals_full_width_text_and_paths(self) -> None:
        """Changing scan order, normalization, or separators must break literal tokens."""
        self.assertEqual(
            (
                "后",
                "端",
                "授",
                "权",
                "后端",
                "端授",
                "授权",
                "cleanupownresources",
                "或",
                "cleanupanyresources",
            ),
            tokenize("后端授权 cleanupOwnResources 或 cleanupAnyResources"),
        )
        self.assertEqual(("arm64", "清", "理", "清理"), tokenize("ＡＲＭ６４ 清理"))
        self.assertEqual(
            ("api", "security", "instance", "actions", "ts"),
            tokenize("api/security-instance-actions.ts"),
        )

    def test_project_decision_exposes_only_formal_retrieval_fields(self) -> None:
        """Private provenance, identities, and generic shared paths must not reach models."""
        revision = _cleanup_revision()

        projected = project_decision(revision)

        self.assertEqual(CLEANUP_PATH.read_bytes(), projected.canonical_bytes)
        self.assertEqual(EXPECTED_EMBEDDING, projected.embedding_text)
        self.assertEqual(EXPECTED_RERANKER, projected.reranker_text)
        self.assertFalse(projected.embedding_text.startswith("passage:"))
        self.assertFalse(projected.reranker_text.startswith("passage:"))
        for forbidden in (
            CLEANUP_ID,
            revision.publication_preview_id,
            revision.source.thread_id,
            revision.source.turn_id,
            revision.review_approval.thread_id,
            revision.review_approval.turn_id,
            "packages/products/third-party-services/apps/security-services/src/",
            "domain/security-services/index.tsx",
        ):
            self.assertNotIn(forbidden, projected.embedding_text)
            self.assertNotIn(forbidden, projected.reranker_text)
        self.assertEqual(
            (
                "packages/products/third-party-services/apps/security-services/src/api/security-capabilities.ts",
                "packages/products/third-party-services/apps/security-services/src/api/security-instance-actions.ts",
                "packages/products/third-party-services/apps/security-services/src/domain/security-services/index.tsx",
            ),
            projected.exact_paths,
        )

    def test_project_query_preserves_negation_but_excludes_it_from_positive_tokens(self) -> None:
        """Removing exclusions from model text or adding them to BM25 must fail."""
        projected = project_query(_cleanup_intent())

        self.assertEqual(EXPECTED_QUERY, projected.embedding_text)
        self.assertEqual(EXPECTED_QUERY, projected.reranker_text)
        self.assertFalse(projected.embedding_text.startswith("query:"))
        self.assertEqual(
            (
                "packages/products/third-party-services/apps/security-services/src/api/security-instance-actions.ts",
            ),
            projected.exact_paths,
        )
        self.assertIn("cleanupownresources", projected.lexical_tokens)
        self.assertEqual(1, projected.lexical_tokens.count("cleanupownresources"))
        self.assertNotIn("重试", projected.lexical_tokens)
        self.assertNotIn("流", projected.lexical_tokens)
        self.assertNotIn("流程", projected.lexical_tokens)


if __name__ == "__main__":
    unittest.main()
