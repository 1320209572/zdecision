"""Strict model-output contract for decision extraction."""

from __future__ import annotations

import json

from zdecision.ids import CAPTURE_EXTRACTOR_VERSION


EXTRACTOR_VERSION = CAPTURE_EXTRACTOR_VERSION
INVENTORY_CONTRACT_VERSION = "inventory-v1"
CANDIDATE_CONTRACT_VERSION = "candidate-v1"


def _inventory_schema() -> dict[str, object]:
    return {
        "signals": [
            {
                "topic": "稳定主题",
                "rule": "一个原子的业务规则",
                "future_effect": "它如何影响未来产品、开发或用户行为",
                "scope": "规则适用范围",
                "status": "current_confirmed",
                "confirmation_basis": "explicit_user_confirmation",
                "confidence": "high",
            }
        ],
        "coverage": {
            "reviewed_retained_context": "earliest_to_latest",
            "known_gaps": [],
        },
    }


def _candidate_schema(product: str) -> dict[str, object]:
    return {
        "candidates": [
            {
                "product": product,
                "claim": "简洁、已确认且长期有效的决策",
                "future_action": "未来工作必须采取或避免的动作",
                "scope": {
                    "summary": "决策适用范围",
                    "repositories": [],
                    "paths": [],
                },
                "invalidation_conditions": [],
            }
        ]
    }


def inventory_schema_json() -> str:
    return json.dumps(_inventory_schema(), ensure_ascii=False, indent=2)


def candidate_schema_json(product: str) -> str:
    return json.dumps(_candidate_schema(product), ensure_ascii=False, indent=2)


def _extraction_schema(product: str) -> dict[str, object]:
    return {
        "candidates": [
            {
                "product": product,
                "claim": "A concise confirmed decision",
                "future_action": "What future work must do",
                "scope": {
                    "summary": "Where the decision applies",
                    "repositories": ["optional canonical remote"],
                    "paths": ["optional/path"],
                },
                "invalidation_conditions": ["Condition that requires review"],
            }
        ]
    }


EXTRACTION_SCHEMA = _extraction_schema("anheng")


def build_extraction_prompt(product: str) -> str:
    """Build the instruction sent to the isolated Capture task."""

    schema = json.dumps(_extraction_schema(product), ensure_ascii=False, indent=2)
    return (
        "Extract only decisions that were explicitly confirmed in the completed "
        "source-task boundary.\n"
        f"Set every candidate product exactly to {product!r}; express narrower "
        "applicability in scope.\n"
        "Zero Candidates is a valid result when no confirmed durable decision "
        "exists.\n"
        "Do not include raw quotations, evidence excerpts, source messages, or a "
        "conversation summary.\n"
        "Return JSON only, with exactly this shape and no additional fields:\n"
        f"{schema}"
    )
