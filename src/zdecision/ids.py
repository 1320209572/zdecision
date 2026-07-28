"""Stable identifiers owned by ZDecision."""

from __future__ import annotations

import hashlib


CAPTURE_EXTRACTOR_VERSION = "extractor-v1"


def capture_operation_id(
    source_thread_id: str,
    source_turn_id: str,
    product: str,
) -> str:
    """Return the stable identity for one Capture boundary."""

    payload = "\n".join(
        (
            source_thread_id,
            source_turn_id,
            product,
            CAPTURE_EXTRACTOR_VERSION,
        )
    ).encode("utf-8")
    return f"cap_{hashlib.sha256(payload).hexdigest()[:32]}"
