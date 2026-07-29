"""Stable identifiers owned by ZDecision."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from zdecision.jsonio import canonical_json_bytes

if TYPE_CHECKING:
    from zdecision.capture.templates import TemplateSnapshot


CAPTURE_EXTRACTOR_VERSION = "extractor-v2"


def capture_operation_id(
    source_thread_id: str,
    source_turn_id: str,
    product: str,
    template: TemplateSnapshot,
) -> str:
    """Return the stable identity for one Capture boundary."""

    payload = canonical_json_bytes(
        {
            "extractor_version": CAPTURE_EXTRACTOR_VERSION,
            "product": product,
            "prompt_bundle_sha256": template.prompt_bundle_sha256,
            "source_thread_id": source_thread_id,
            "source_turn_id": source_turn_id,
            "template_id": template.template_id,
            "template_revision": template.revision,
            "template_source_sha256": template.template_source_sha256,
        }
    )
    return f"cap_{hashlib.sha256(payload).hexdigest()[:32]}"


def capture_candidate_id(operation_id: str, ordinal: int) -> str:
    """Return the deterministic Candidate id for one V2 Capture ordinal."""

    return f"cand_{operation_id.removeprefix('cap_')}_{ordinal:02d}"
