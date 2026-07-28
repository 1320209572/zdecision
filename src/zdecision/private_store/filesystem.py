"""Filesystem-backed private state for the single-user V1."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from zdecision.capture.models import Candidate, CaptureRecord
from zdecision.jsonio import atomic_write_json


_SAFE_OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidPrivateObjectId(ValueError):
    """Raised when an object id could escape its owned directory."""


def private_state_root(environ: Mapping[str, str]) -> Path:
    """Resolve the user-local private state root for the current platform."""

    override = environ.get("ZDECISION_STATE_DIR")
    if override:
        return Path(override).expanduser()

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ZDecision"
    if os.name == "nt":
        local_app_data = environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "ZDecision"
        return Path.home() / "AppData" / "Local" / "ZDecision"

    xdg_state_home = environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "zdecision"
    return Path.home() / ".local" / "state" / "zdecision"


class FilePrivateStore:
    """Persist typed private objects outside the Git Registry."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put_capture(self, capture: CaptureRecord) -> None:
        atomic_write_json(
            self._object_path("captures", capture.operation_id),
            capture.to_dict(),
        )

    def get_capture(self, operation_id: str) -> CaptureRecord | None:
        path = self._object_path("captures", operation_id)
        if not path.exists():
            return None
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Capture private state must be a JSON object")
        return CaptureRecord.from_dict(value)

    def put_candidate(self, candidate: Candidate) -> None:
        atomic_write_json(
            self._object_path("candidates", candidate.candidate_id),
            candidate.to_dict(),
        )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        path = self._object_path("candidates", candidate_id)
        if not path.exists():
            return None
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Candidate private state must be a JSON object")
        return Candidate.from_dict(value)

    def _object_path(self, collection: str, object_id: str) -> Path:
        if not object_id or not _SAFE_OBJECT_ID.fullmatch(object_id):
            raise InvalidPrivateObjectId(f"Unsafe private object id: {object_id!r}")
        return self.root / collection / f"{object_id}.json"
