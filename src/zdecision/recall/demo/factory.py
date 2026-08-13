"""Dependency-light selection boundary for the optional Recall Demo provider."""

from __future__ import annotations

from pathlib import Path

from zdecision.recall.demo.config import load_demo_recall_config
from zdecision.recall.provider import RecallProvider, UnavailableRecallProvider


def configured_recall_provider(path: Path) -> RecallProvider:
    """Load optional Demo code only after owner-only configuration is valid."""
    try:
        config = load_demo_recall_config(Path(path))
    except Exception:
        return UnavailableRecallProvider()
    try:
        from zdecision.recall.demo.provider import DemoRecallProvider

        return DemoRecallProvider(config.provider)
    except Exception:
        return UnavailableRecallProvider()
