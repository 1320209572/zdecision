"""Filesystem-backed private state for the single-user V1."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from zdecision.capture.inventory import InventoryResult, validate_inventory
from zdecision.capture.models import (
    LEGACY_CAPTURE_FIELDS,
    Candidate,
    CaptureRecord,
    ExtractionManifest,
    LegacyCaptureRecord,
)
from zdecision.jsonio import (
    atomic_create_json,
    atomic_write_json,
    canonical_json_bytes,
)


_SAFE_OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidPrivateObjectId(ValueError):
    """Raised when an object id could escape its owned directory."""


class PrivateStateCorrupt(Exception):
    """Sanitized boundary for malformed or internally inconsistent state."""

    def __init__(self, collection: str, object_id: str) -> None:
        self.collection = collection
        self.object_id = object_id
        super().__init__(f"Private {collection} object {object_id!r} is invalid")


class PrivateStateConflict(Exception):
    """Raised when immutable private state would be replaced."""


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
        if not isinstance(capture, CaptureRecord):
            raise TypeError("Only extractor-v2 Capture records may be written")
        CaptureRecord.from_dict(capture.to_dict())
        atomic_write_json(
            self._object_path("captures", capture.operation_id),
            capture.to_dict(),
        )

    def get_capture(
        self, operation_id: str
    ) -> CaptureRecord | LegacyCaptureRecord | None:
        path = self._object_path("captures", operation_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Capture private state must be a JSON object")
            if frozenset(value) == LEGACY_CAPTURE_FIELDS:
                record: CaptureRecord | LegacyCaptureRecord = (
                    LegacyCaptureRecord.from_dict(value)
                )
            else:
                record = CaptureRecord.from_dict(value)
            if record.operation_id != operation_id:
                raise ValueError("Capture object identity mismatch")
            return record
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PrivateStateCorrupt("captures", operation_id) from None

    def put_candidate(self, candidate: Candidate) -> None:
        if not isinstance(candidate, Candidate):
            raise TypeError("Only validated Candidate values may be written")
        Candidate.from_dict(candidate.to_dict())
        path = self._object_path("candidates", candidate.candidate_id)
        if atomic_create_json(path, candidate.to_dict()):
            return
        existing = self.get_candidate(candidate.candidate_id)
        if existing is not None and path.read_bytes() == canonical_json_bytes(
            candidate.to_dict()
        ):
            return
        raise PrivateStateConflict(
            f"Private Candidate {candidate.candidate_id!r} already has "
            "different content"
        )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        path = self._object_path("candidates", candidate_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Candidate private state must be a JSON object")
            candidate = Candidate.from_dict(value)
            if candidate.candidate_id != candidate_id:
                raise ValueError("Candidate object identity mismatch")
            return candidate
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PrivateStateCorrupt("candidates", candidate_id) from None

    def put_extraction_manifest(self, manifest: ExtractionManifest) -> None:
        if not isinstance(manifest, ExtractionManifest):
            raise TypeError("Only validated ExtractionManifest values may be written")
        ExtractionManifest.from_dict(manifest.to_dict())
        path = self._object_path("extraction_manifests", manifest.operation_id)
        if not path.exists() and self.candidate_ids_for_capture(
            manifest.operation_id
        ):
            raise PrivateStateConflict(
                f"Capture {manifest.operation_id!r} has unowned Candidate state"
            )
        if atomic_create_json(path, manifest.to_dict()):
            return
        existing = self.get_extraction_manifest(manifest.operation_id)
        if existing is not None and canonical_json_bytes(
            existing.to_dict()
        ) == canonical_json_bytes(manifest.to_dict()):
            return
        raise PrivateStateConflict(
            f"Private extraction manifest {manifest.operation_id!r} already "
            "has different content"
        )

    def get_extraction_manifest(
        self, operation_id: str
    ) -> ExtractionManifest | None:
        path = self._object_path("extraction_manifests", operation_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Extraction manifest must be a JSON object")
            manifest = ExtractionManifest.from_dict(value)
            if manifest.operation_id != operation_id:
                raise ValueError("Extraction manifest identity mismatch")
            return manifest
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PrivateStateCorrupt("extraction_manifests", operation_id) from None

    def candidate_ids_for_capture(self, operation_id: str) -> tuple[str, ...]:
        self._object_path("captures", operation_id)
        directory = self.root / "candidates"
        if not directory.exists():
            return ()
        prefix = f"cand_{operation_id.removeprefix('cap_')}_"
        return tuple(
            sorted(
                path.stem
                for path in directory.iterdir()
                if path.is_file()
                and path.suffix == ".json"
                and path.stem.startswith(prefix)
            )
        )

    def put_inventory(
        self,
        operation_id: str,
        inventory: InventoryResult,
    ) -> None:
        if not isinstance(inventory, InventoryResult):
            raise TypeError("Only validated InventoryResult values may be written")
        path = self._object_path("inventories", operation_id)
        if path.exists():
            existing = self.get_inventory(operation_id)
            assert existing is not None
            if canonical_json_bytes(existing.to_dict()) == canonical_json_bytes(
                inventory.to_dict()
            ):
                return
            raise PrivateStateConflict(
                f"Private inventory {operation_id!r} already has different content"
            )
        atomic_write_json(path, inventory.to_dict())

    def get_inventory(self, operation_id: str) -> InventoryResult | None:
        path = self._object_path("inventories", operation_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
            return validate_inventory(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PrivateStateCorrupt("inventories", operation_id) from None

    def _object_path(self, collection: str, object_id: str) -> Path:
        if not object_id or not _SAFE_OBJECT_ID.fullmatch(object_id):
            raise InvalidPrivateObjectId(f"Unsafe private object id: {object_id!r}")
        return self.root / collection / f"{object_id}.json"
