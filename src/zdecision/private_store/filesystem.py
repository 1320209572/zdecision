"""Filesystem-backed private state for the single-user V1."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Collection, Mapping
from pathlib import Path

from zdecision.capture.inventory import InventoryResult, validate_inventory
from zdecision.capture.models import (
    LEGACY_CAPTURE_FIELDS,
    Candidate,
    CaptureRecord,
    ExtractionManifest,
    LegacyCaptureRecord,
)
from zdecision.capture.reviews import ReviewBatch
from zdecision.jsonio import (
    atomic_create_json,
    atomic_write_json,
    canonical_json_bytes,
)
from zdecision.registry.publication import (
    CandidatePublicationReceipt,
    PublicationRecord,
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

    def put_review_batch(self, batch: ReviewBatch) -> None:
        if not isinstance(batch, ReviewBatch):
            raise TypeError("Only validated ReviewBatch values may be written")
        ReviewBatch.from_dict(batch.to_dict())
        path = self._object_path("review_batches", batch.review_batch_id)
        if atomic_create_json(path, batch.to_dict()):
            return
        existing = self.get_review_batch(batch.review_batch_id)
        if existing is not None and path.read_bytes() == canonical_json_bytes(
            batch.to_dict()
        ):
            return
        raise PrivateStateConflict(
            f"Private Review batch {batch.review_batch_id!r} already has "
            "different content"
        )

    def get_review_batch(self, batch_id: str) -> ReviewBatch | None:
        path = self._object_path("review_batches", batch_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Review batch private state must be an object")
            batch = ReviewBatch.from_dict(value)
            if batch.review_batch_id != batch_id:
                raise ValueError("Review batch object identity mismatch")
            return batch
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PrivateStateCorrupt("review_batches", batch_id) from None

    def review_batch_ids_for_capture(self, capture_id: str) -> tuple[str, ...]:
        self._object_path("captures", capture_id)
        return tuple(
            batch_id
            for batch_id in self._review_batch_ids()
            if self._required_review_batch(batch_id).capture_id == capture_id
        )

    def review_batch_for_approval(
        self,
        thread_id: str,
        turn_id: str,
    ) -> ReviewBatch | None:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("Review approval thread id must not be empty")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("Review approval turn id must not be empty")
        matched: ReviewBatch | None = None
        for batch_id in self._review_batch_ids():
            batch = self._required_review_batch(batch_id)
            if (
                batch.approval.thread_id != thread_id
                or batch.approval.turn_id != turn_id
            ):
                continue
            if matched is not None:
                raise PrivateStateCorrupt("review_batches", "approval")
            matched = batch
        return matched

    def create_publication(self, record: PublicationRecord) -> PublicationRecord:
        if not isinstance(record, PublicationRecord):
            raise TypeError("Only validated Publication records may be written")
        PublicationRecord.from_dict(record.to_dict())
        path = self._object_path("publications", record.preview_id)
        if atomic_create_json(path, record.to_dict()):
            return record
        if path.read_bytes() == canonical_json_bytes(record.to_dict()):
            existing = self.get_publication(record.preview_id)
            if existing is not None:
                return existing
        raise PrivateStateConflict(
            f"Private publication {record.preview_id!r} already has different content"
        )

    def get_publication(self, preview_id: str) -> PublicationRecord | None:
        path = self._object_path("publications", preview_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Publication private state must be an object")
            record = PublicationRecord.from_dict(value)
            if record.preview_id != preview_id:
                raise ValueError("Publication object identity mismatch")
            if path.read_bytes() != canonical_json_bytes(record.to_dict()):
                raise ValueError("Publication private state is not canonical")
            return record
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PrivateStateCorrupt("publications", preview_id) from None

    def replace_publication(
        self,
        expected: PublicationRecord,
        replacement: PublicationRecord,
    ) -> PublicationRecord:
        if not isinstance(expected, PublicationRecord) or not isinstance(
            replacement, PublicationRecord
        ):
            raise TypeError("Publication replacement requires validated records")
        if expected.preview_id != replacement.preview_id:
            raise PrivateStateConflict("Publication identity cannot be replaced")
        self._require_same_publication_payload(expected, replacement)
        allowed = {
            "previewed": "confirmed",
            "confirmed": "committed_pending_push",
            "committed_pending_push": "completed",
        }
        if replacement != expected and allowed.get(expected.state) != replacement.state:
            raise PrivateStateConflict("Publication state transition is invalid")

        path = self._object_path("publications", expected.preview_id)
        if not path.exists():
            raise PrivateStateConflict("Publication private state does not exist")
        current_bytes = path.read_bytes()
        replacement_bytes = canonical_json_bytes(replacement.to_dict())
        if current_bytes == replacement_bytes:
            return replacement
        if current_bytes != canonical_json_bytes(expected.to_dict()):
            raise PrivateStateConflict("Publication private state changed")
        atomic_write_json(path, replacement.to_dict())
        return replacement

    def publication_ids_for_candidates(
        self,
        candidate_ids: Collection[str],
    ) -> tuple[str, ...]:
        if isinstance(candidate_ids, (str, bytes)) or not isinstance(
            candidate_ids, Collection
        ):
            raise ValueError("Candidate ids must be a collection")
        selected = set(candidate_ids)
        for candidate_id in selected:
            self._object_path("candidate_publication_receipts", candidate_id)
            if re.fullmatch(
                r"cand_[0-9a-f]{32}_(?:0[1-9]|1[0-9]|20)", candidate_id
            ) is None:
                raise ValueError("Candidate id is invalid")
        if not selected:
            return ()
        return tuple(
            preview_id
            for preview_id in self._publication_ids()
            if selected.intersection(
                self._required_publication(preview_id).candidate_ids
            )
        )

    def put_candidate_receipt(
        self,
        receipt: CandidatePublicationReceipt,
    ) -> None:
        if not isinstance(receipt, CandidatePublicationReceipt):
            raise TypeError("Only validated Candidate publication receipts may be written")
        CandidatePublicationReceipt.from_dict(receipt.to_dict())
        path = self._object_path(
            "candidate_publication_receipts", receipt.candidate_id
        )
        if atomic_create_json(path, receipt.to_dict()):
            return
        existing = self.get_candidate_receipt(receipt.candidate_id)
        if existing is not None and path.read_bytes() == canonical_json_bytes(
            receipt.to_dict()
        ):
            return
        raise PrivateStateConflict(
            f"Candidate {receipt.candidate_id!r} already has a publication receipt"
        )

    def get_candidate_receipt(
        self,
        candidate_id: str,
    ) -> CandidatePublicationReceipt | None:
        path = self._object_path(
            "candidate_publication_receipts", candidate_id
        )
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Candidate publication receipt must be an object")
            receipt = CandidatePublicationReceipt.from_dict(value)
            if receipt.candidate_id != candidate_id:
                raise ValueError("Candidate publication receipt identity mismatch")
            if path.read_bytes() != canonical_json_bytes(receipt.to_dict()):
                raise ValueError("Candidate publication receipt is not canonical")
            return receipt
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PrivateStateCorrupt(
                "candidate_publication_receipts", candidate_id
            ) from None

    @staticmethod
    def _require_same_publication_payload(
        expected: PublicationRecord,
        replacement: PublicationRecord,
    ) -> None:
        mutable_fields = frozenset(
            ("state", "publication_approval", "commit_sha")
        )
        expected_value = {
            key: value
            for key, value in expected.to_dict().items()
            if key not in mutable_fields
        }
        replacement_value = {
            key: value
            for key, value in replacement.to_dict().items()
            if key not in mutable_fields
        }
        if expected_value != replacement_value:
            raise PrivateStateConflict("Publication preview bytes are immutable")

    def _publication_ids(self) -> tuple[str, ...]:
        directory = self.root / "publications"
        if not directory.exists():
            return ()
        return tuple(
            sorted(
                path.stem
                for path in directory.iterdir()
                if path.is_file()
                and path.suffix == ".json"
                and re.fullmatch(r"pub_[0-9a-f]{32}", path.stem) is not None
            )
        )

    def _required_publication(self, preview_id: str) -> PublicationRecord:
        record = self.get_publication(preview_id)
        if record is None:
            raise PrivateStateCorrupt("publications", preview_id)
        return record

    def _review_batch_ids(self) -> tuple[str, ...]:
        directory = self.root / "review_batches"
        if not directory.exists():
            return ()
        return tuple(
            sorted(
                path.stem
                for path in directory.iterdir()
                if path.is_file()
                and path.suffix == ".json"
                and re.fullmatch(r"rvb_[0-9a-f]{32}", path.stem) is not None
            )
        )

    def _required_review_batch(self, batch_id: str) -> ReviewBatch:
        batch = self.get_review_batch(batch_id)
        if batch is None:
            raise PrivateStateCorrupt("review_batches", batch_id)
        return batch

    def _object_path(self, collection: str, object_id: str) -> Path:
        if not object_id or not _SAFE_OBJECT_ID.fullmatch(object_id):
            raise InvalidPrivateObjectId(f"Unsafe private object id: {object_id!r}")
        return self.root / collection / f"{object_id}.json"
