"""Fail-closed provider seam for formal Recall."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from zdecision.recall.handoff import (
    RecallPreflightReady,
    RecallPreflightResult,
    RecallPreflightUnavailable,
    RecallShortlist,
)
from zdecision.recall.session import RecallIntent


class RecallProviderUnavailable(RuntimeError):
    """Raised when production Recall retrieval is not available."""


class RecallProvider(Protocol):
    def preflight(
        self,
        *,
        repository_id: str,
        repository_display_name: str,
        intent: RecallIntent,
        now: datetime,
    ) -> RecallPreflightResult:
        raise NotImplementedError

    def retrieve(self, preflight: RecallPreflightReady) -> RecallShortlist:
        raise NotImplementedError


class UnavailableRecallProvider:
    """Production default until a trusted Recall provider is installed."""

    def preflight(
        self,
        *,
        repository_id: str,
        repository_display_name: str,
        intent: RecallIntent,
        now: datetime,
    ) -> RecallPreflightUnavailable:
        return RecallPreflightUnavailable(code="recall_not_ready")

    def retrieve(self, preflight: RecallPreflightReady) -> RecallShortlist:
        raise RecallProviderUnavailable("Recall provider is unavailable")
