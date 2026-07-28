"""Strict model-output contract for decision extraction."""

from __future__ import annotations

import json

from zdecision.ids import CAPTURE_EXTRACTOR_VERSION


EXTRACTOR_VERSION = CAPTURE_EXTRACTOR_VERSION


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
