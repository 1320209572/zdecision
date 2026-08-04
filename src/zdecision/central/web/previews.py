"""Immutable, exact central publication Preview creation and reads."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from zdecision.capture.models import SourceCheckpoint
from zdecision.central.auth import Principal
from zdecision.central.web.contracts import CentralReviewBatch, CentralReviewItem
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import (
    CentralWebStore,
    WebActionConflict,
    WebRecordCorrupt,
    immediate,
)
from zdecision.ids import (
    PUBLISHER_FORMAT_VERSION,
    decision_id,
    publication_preview_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.catalog import RegistryCatalog, RegistryError
from zdecision.registry.git import (
    GitRegistryAdapter,
    GitRegistryError,
    RegistryOutOfSync,
)
from zdecision.registry.models import DecisionRevision, DecisionSeed
from zdecision.registry.publication import (
    PublicationFile,
    PublicationRecord,
    content_digest_for_files,
)


Publishability = Literal["publishable", "stale", "registry_unavailable"]
_ARCHIVE_LIMIT = 16 * 1024 * 1024


class CentralPreviewError(Exception):
    code = "preview_error"


class PreviewNotFound(CentralPreviewError):
    code = "not_found"


class NoAcceptedItems(CentralPreviewError):
    code = "no_accepted_items"


class PreviewStale(CentralPreviewError):
    code = "preview_stale"


class RegistryUnavailable(CentralPreviewError):
    code = "registry_unavailable"


@dataclass(frozen=True)
class PublicationPreviewView:
    record: PublicationRecord
    publishability: Publishability
    publication_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.record, PublicationRecord):
            raise TypeError("record must be a PublicationRecord")
        if self.publishability not in (
            "publishable", "stale", "registry_unavailable"
        ):
            raise ValueError("publishability is invalid")
        if self.publication_id is not None and not self.publication_id.startswith(
            "plb_"
        ):
            raise ValueError("publication_id is invalid")

    def to_dict(self) -> dict[str, object]:
        value = self.record.to_dict()
        decisions_by_id: dict[str, dict[str, object]] = {}
        decision_ids = set(self.record.decision_ids)
        for document in self.record.display_documents:
            try:
                decoded = json.loads(document.content)
                decision = DecisionRevision.from_dict(decoded)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if decision.decision_id not in decision_ids:
                continue
            decisions_by_id[decision.decision_id] = (
                {
                    "path": document.path,
                    "sha256": document.sha256,
                    "canonical_json": document.content,
                    **decision.to_dict(),
                }
            )
        value.update(
            {
                "publishability": self.publishability,
                "publication_id": self.publication_id,
                "decisions": [
                    decisions_by_id[decision_id]
                    for decision_id in self.record.decision_ids
                    if decision_id in decisions_by_id
                ],
            }
        )
        return value


class CentralPreviewService:
    def __init__(
        self,
        *,
        store: CentralWebStore,
        queries: CentralWebQueries,
        catalog: RegistryCatalog,
        git: GitRegistryAdapter,
    ) -> None:
        if not isinstance(store, CentralWebStore):
            raise TypeError("store must be a CentralWebStore")
        if not isinstance(queries, CentralWebQueries):
            raise TypeError("queries must be CentralWebQueries")
        if not isinstance(catalog, RegistryCatalog):
            raise TypeError("catalog must be a RegistryCatalog")
        if not isinstance(git, GitRegistryAdapter):
            raise TypeError("git must be a GitRegistryAdapter")
        if catalog.repository_root != git.repository_root:
            raise ValueError("Registry roots do not match")
        self.store = store
        self.queries = queries
        self.catalog = catalog
        self.git = git

    def create(
        self,
        principal: Principal,
        review_batch_id: str,
        client_action_id: str,
        now: str | datetime,
    ) -> PublicationPreviewView:
        self._require_user(principal)
        timestamp = self._timestamp(now)
        request_digest = hashlib.sha256(
            canonical_json_bytes({"review_batch_id": review_batch_id})
        ).hexdigest()
        replay = self.store.action_result(
            principal.organization_id,
            principal.actor_id,
            "preview",
            client_action_id,
        )
        if replay is not None:
            if replay.request_digest != request_digest:
                raise WebActionConflict("web_action_conflict")
            return self.get(principal, replay.result_id)

        batch = self._owned_batch(principal, review_batch_id)
        accepted = self._accepted(batch)
        if not accepted:
            raise NoAcceptedItems("no_accepted_items")
        self._require_latest_and_unpublished(principal, batch, accepted)
        seeds = tuple(self._seed(batch, item) for item in accepted)
        try:
            base_commit = self.git.fetch_and_require_exact_main()
            self.git.require_clean_registry()
            with self._committed_catalog(base_commit) as catalog:
                plan = catalog.inspect(seeds)
                preview_id = publication_preview_id(
                    {
                        "base_commit": base_commit,
                        "base_registry_digests": plan.base_registry_digests,
                        "decision_ids": plan.decision_ids,
                        "publisher_format": PUBLISHER_FORMAT_VERSION,
                        "review_ids": tuple(item.review_id for item in accepted),
                        "target_paths": plan.changed_paths,
                    }
                )
                draft = catalog.render(plan, preview_id)
        except (
            GitRegistryError,
            RegistryError,
            OSError,
            subprocess.SubprocessError,
            tarfile.TarError,
        ):
            raise RegistryUnavailable("registry_unavailable") from None

        display_files = tuple(
            PublicationFile.from_bytes(path, data)
            for path, data in sorted(draft.display_documents.items())
        )
        changed_files = tuple(
            PublicationFile.from_bytes(path, data)
            for path, data in sorted(draft.changed_files.items())
        )
        record = PublicationRecord(
            record_version=1,
            preview_id=preview_id,
            content_digest=content_digest_for_files(display_files),
            state="previewed",
            created_at=timestamp,
            review_batch_id=batch.review_batch_id,
            review_ids=tuple(item.review_id for item in accepted),
            candidate_ids=tuple(
                item.publication_candidate_id for item in accepted
            ),
            decision_ids=plan.decision_ids,
            product_id=batch.product_id,
            product_name=batch.product_name,
            base_commit=base_commit,
            base_registry_digests=plan.base_registry_digests,
            display_documents=display_files,
            changed_files=changed_files,
            commit_message=(
                f"decision({batch.product_id}): publish {len(accepted)} decisions\n\n"
                f"ZDecision-Preview: {preview_id}\n"
            ),
        )
        replayed_preview_id: str | None = None
        stored: PublicationRecord | None = None
        with immediate(self.store.connection):
            replay = self.store.action_result(
                principal.organization_id,
                principal.actor_id,
                "preview",
                client_action_id,
            )
            if replay is not None:
                if replay.request_digest != request_digest:
                    raise WebActionConflict("web_action_conflict")
                existing = self.store.get_preview(
                    principal.organization_id, replay.result_id
                )
                if existing is None:
                    raise WebRecordCorrupt("preview_action_result")
                replayed_preview_id = existing.preview_id
            else:
                self._require_latest_and_unpublished(principal, batch, accepted)
                try:
                    self.git.fetch_and_require_exact_main(base_commit)
                except GitRegistryError:
                    raise RegistryUnavailable("registry_unavailable") from None
                existing = self.store.get_preview(
                    principal.organization_id, record.preview_id
                )
                if existing is not None:
                    record = replace(record, created_at=existing.created_at)
                stored = self.store.put_preview(
                    principal.organization_id, batch.product_id, record
                )
                self.store.record_action(
                    principal.organization_id,
                    principal.actor_id,
                    "preview",
                    client_action_id,
                    request_digest,
                    stored.preview_id,
                    timestamp,
                )
        if replayed_preview_id is not None:
            return self.get(principal, replayed_preview_id)
        if stored is None:
            raise WebRecordCorrupt("preview_action_result")
        return PublicationPreviewView(stored, "publishable", None)

    def get(
        self, principal: Principal, preview_id: str
    ) -> PublicationPreviewView:
        self._require_user(principal)
        record = self.store.get_preview(principal.organization_id, preview_id)
        if record is None:
            raise PreviewNotFound("not_found")
        batch = self._owned_batch(principal, record.review_batch_id)
        publication = self.store.get_publication_by_preview(
            principal.organization_id, preview_id
        )
        return PublicationPreviewView(
            record=record,
            publishability=self.check_publishability(principal, batch, record),
            publication_id=(
                publication.publication_id if publication is not None else None
            ),
        )

    def check_publishability(
        self,
        principal: Principal,
        batch: CentralReviewBatch,
        record: PublicationRecord,
    ) -> Publishability:
        try:
            self._require_fresh(principal, batch, record)
        except PreviewStale:
            return "stale"
        except RegistryOutOfSync:
            return (
                "stale"
                if self._known_registry_base_changed(record.base_commit)
                else "registry_unavailable"
            )
        except (
            GitRegistryError,
            RegistryError,
            OSError,
            subprocess.SubprocessError,
            tarfile.TarError,
        ):
            return "registry_unavailable"
        return "publishable"

    def require_current_central_state(
        self,
        principal: Principal,
        record: PublicationRecord,
    ) -> None:
        """Revalidate mutable Central state at the confirmation boundary."""

        self._require_user(principal)
        batch = self._owned_batch(principal, record.review_batch_id)
        accepted = self._accepted(batch)
        if not accepted:
            raise PreviewStale("preview_stale")
        self._require_latest_and_unpublished(principal, batch, accepted)
        if (
            tuple(item.review_id for item in accepted) != record.review_ids
            or tuple(item.publication_candidate_id for item in accepted)
            != record.candidate_ids
        ):
            raise PreviewStale("preview_stale")

    def _known_registry_base_changed(self, expected_base: str) -> bool:
        values: list[str] = []
        for revision in ("HEAD", "refs/remotes/origin/main"):
            try:
                result = subprocess.run(
                    (
                        "git",
                        "-C",
                        str(self.git.repository_root),
                        "--no-replace-objects",
                        "rev-parse",
                        "--verify",
                        revision,
                    ),
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            value = result.stdout.decode("ascii", errors="replace").strip()
            if result.returncode != 0 or len(value) != 40:
                return False
            values.append(value)
        return any(value != expected_base for value in values)

    def _require_fresh(
        self,
        principal: Principal,
        batch: CentralReviewBatch,
        record: PublicationRecord,
    ) -> None:
        accepted = self._accepted(batch)
        if not accepted:
            raise PreviewStale("preview_stale")
        self._require_latest_and_unpublished(principal, batch, accepted)
        seeds = tuple(self._seed(batch, item) for item in accepted)
        self.git.fetch_and_require_exact_main(record.base_commit)
        self.git.require_clean_registry()
        with self._committed_catalog(record.base_commit) as catalog:
            plan = catalog.inspect(seeds)
            draft = catalog.render(plan, record.preview_id)
        if (
            tuple(item.review_id for item in accepted) != record.review_ids
            or tuple(item.publication_candidate_id for item in accepted)
            != record.candidate_ids
            or plan.decision_ids != record.decision_ids
            or dict(plan.base_registry_digests)
            != dict(record.base_registry_digests)
            or plan.changed_paths
            != tuple(file.path for file in record.changed_files)
            or dict(draft.display_documents) != record.display_file_bytes()
            or dict(draft.changed_files) != record.changed_file_bytes()
        ):
            raise PreviewStale("preview_stale")

    def _require_latest_and_unpublished(
        self,
        principal: Principal,
        batch: CentralReviewBatch,
        accepted: tuple[CentralReviewItem, ...],
    ) -> None:
        families = tuple(item.family_id for item in accepted)
        latest = self.store.latest_review_ids(
            principal.organization_id, batch.product_id, families
        )
        if any(latest.get(item.family_id) != item.review_id for item in accepted):
            raise PreviewStale("preview_stale")
        if self.store.published_families(
            principal.organization_id, batch.product_id, families
        ):
            raise PreviewStale("preview_stale")
        for item in accepted:
            current = self.queries.current_candidate_revision(
                principal, item.repository_id, item.family_id
            )
            if current is None or (
                current.revision_id != item.revision_id
                or current.revision != item.revision
                or current.content_digest != item.content_digest
            ):
                raise PreviewStale("preview_stale")

    def _owned_batch(
        self, principal: Principal, review_batch_id: str
    ) -> CentralReviewBatch:
        batch = self.store.get_review_batch_by_id(
            principal.organization_id, review_batch_id
        )
        if batch is None or batch.actor_id != principal.actor_id:
            raise PreviewNotFound("not_found")
        return batch

    @staticmethod
    def _accepted(
        batch: CentralReviewBatch,
    ) -> tuple[CentralReviewItem, ...]:
        return tuple(
            item
            for item in batch.items
            if item.action in ("accept", "edit_accept")
        )

    @staticmethod
    def _seed(
        batch: CentralReviewBatch, item: CentralReviewItem
    ) -> DecisionSeed:
        if item.action not in ("accept", "edit_accept"):
            raise ValueError("Review item is not accepted")
        if item.effective_content is None:
            raise WebRecordCorrupt("review_item")
        candidate_id = item.publication_candidate_id
        return DecisionSeed(
            candidate_id=candidate_id,
            decision_id=decision_id(candidate_id, batch.product_id),
            product_id=batch.product_id,
            product_name=batch.product_name,
            content=item.effective_content,
            source=SourceCheckpoint(
                thread_id=(
                    f"candidate_family_{item.family_id.removeprefix('cfm_')}"
                ),
                turn_id=(
                    f"candidate_revision_{item.revision_id.removeprefix('crv_')}"
                ),
            ),
            review_approval=batch.approval,
        )

    @contextmanager
    def _committed_catalog(
        self, commit_sha: str
    ) -> Iterator[RegistryCatalog]:
        try:
            archive = subprocess.run(
                (
                    "git",
                    "-C",
                    str(self.git.repository_root),
                    "--no-replace-objects",
                    "archive",
                    "--format=tar",
                    commit_sha,
                    "decision-registry",
                ),
                check=False,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise OSError("Registry commit archive is unavailable") from None
        if archive.returncode != 0 or len(archive.stdout) > _ARCHIVE_LIMIT:
            raise OSError("Registry commit archive is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
                for member in tar.getmembers():
                    pure = PurePosixPath(member.name)
                    if (
                        pure.is_absolute()
                        or not pure.parts
                        or pure.parts[0] != "decision-registry"
                        or any(part in ("", ".", "..") for part in pure.parts)
                        or member.issym()
                        or member.islnk()
                        or not (member.isdir() or member.isfile())
                    ):
                        raise OSError("Registry commit archive is invalid")
                    target = root.joinpath(*pure.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    source = tar.extractfile(member)
                    if source is None:
                        raise OSError("Registry commit archive is invalid")
                    data = source.read(_ARCHIVE_LIMIT + 1)
                    if len(data) > _ARCHIVE_LIMIT:
                        raise OSError("Registry commit archive is invalid")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            yield RegistryCatalog(root)

    @staticmethod
    def _require_user(principal: Principal) -> None:
        if not isinstance(principal, Principal) or principal.kind != "user":
            raise ValueError("A browser user Principal is required")

    @staticmethod
    def _timestamp(value: str | datetime) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("now is invalid")
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("now is invalid")
        datetime.fromisoformat(value[:-1] + "+00:00")
        return value
