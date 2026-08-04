"""Canonical SQLite operations for the central Decision Web."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager

from zdecision.central.auth import require_id
from zdecision.central.web.contracts import (
    ActionKind,
    ActionResult,
    CentralPublication,
    CentralReviewBatch,
    DraftItem,
    ReviewDraft,
)
from zdecision.ids import publication_candidate_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.publication import (
    CandidatePublicationReceipt,
    PublicationRecord,
)


class CentralWebStoreError(Exception):
    """Base error for safe central-Web persistence failures."""


class WebActionConflict(CentralWebStoreError):
    pass


class DraftConflict(CentralWebStoreError):
    pass


class WebRecordConflict(CentralWebStoreError):
    pass


class WebRecordCorrupt(CentralWebStoreError):
    pass


@contextmanager
def immediate(connection: sqlite3.Connection) -> Iterator[None]:
    """Enter one immediate transaction, reusing an owning outer transaction."""

    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _canonical_record(value: object) -> tuple[str, str]:
    encoded = canonical_json_bytes(value)
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _read_record(
    record_json: object,
    record_digest: object,
    record_type: type[object],
    label: str,
) -> object:
    if not isinstance(record_json, str) or not isinstance(record_digest, str):
        raise WebRecordCorrupt(label)
    try:
        value = json.loads(record_json)
        record = record_type.from_dict(value)
        encoded = canonical_json_bytes(record.to_dict())
        if (
            encoded != record_json.encode("utf-8")
            or hashlib.sha256(encoded).hexdigest() != record_digest
        ):
            raise ValueError
        return record
    except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise WebRecordCorrupt(label) from None


class CentralWebStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        self.connection = connection

    def get_draft(
        self, organization_id: str, actor_id: str, product_id: str
    ) -> ReviewDraft:
        organization = require_id(organization_id, "organization_id")
        actor = require_id(actor_id, "actor_id")
        row = self.connection.execute(
            """
            SELECT version, record_json, record_digest
            FROM web_review_drafts
            WHERE organization_id = ? AND actor_id = ? AND product_id = ?
            """,
            (organization, actor, product_id),
        ).fetchone()
        if row is None:
            return ReviewDraft(organization, actor, product_id, 0, (), None)
        draft = _read_record(
            row["record_json"], row["record_digest"], ReviewDraft, "review_draft"
        )
        if (
            draft.organization_id != organization
            or draft.actor_id != actor
            or draft.product_id != product_id
            or draft.version != row["version"]
        ):
            raise WebRecordCorrupt("review_draft")
        return draft

    def replace_draft(
        self,
        expected: ReviewDraft,
        items: Sequence[DraftItem],
        now: str,
    ) -> ReviewDraft:
        if not isinstance(expected, ReviewDraft):
            raise TypeError("expected must be a ReviewDraft")
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise ValueError("items must be a sequence")
        replacement = ReviewDraft(
            expected.organization_id,
            expected.actor_id,
            expected.product_id,
            expected.version + 1,
            tuple(items),
            now,
        )
        record_json, record_digest = _canonical_record(replacement.to_dict())
        with immediate(self.connection):
            current = self.get_draft(
                expected.organization_id, expected.actor_id, expected.product_id
            )
            if current != expected:
                raise DraftConflict("review_draft_conflict")
            if expected.version == 0 and expected.updated_at is None:
                try:
                    self.connection.execute(
                        """
                        INSERT INTO web_review_drafts(
                            organization_id, actor_id, product_id, version,
                            record_json, record_digest, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            replacement.organization_id,
                            replacement.actor_id,
                            replacement.product_id,
                            replacement.version,
                            record_json,
                            record_digest,
                            replacement.updated_at,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise DraftConflict("review_draft_conflict") from error
            else:
                cursor = self.connection.execute(
                    """
                    UPDATE web_review_drafts
                    SET version = ?, record_json = ?, record_digest = ?, updated_at = ?
                    WHERE organization_id = ? AND actor_id = ? AND product_id = ?
                      AND version = ?
                    """,
                    (
                        replacement.version,
                        record_json,
                        record_digest,
                        replacement.updated_at,
                        replacement.organization_id,
                        replacement.actor_id,
                        replacement.product_id,
                        expected.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DraftConflict("review_draft_conflict")
        return replacement

    def clear_submitted_draft_items(
        self,
        draft: ReviewDraft,
        submitted: Collection[DraftItem],
        now: str,
    ) -> ReviewDraft:
        """Remove exactly submitted draft families through the normal CAS path."""

        if not isinstance(draft, ReviewDraft):
            raise TypeError("draft must be a ReviewDraft")
        if isinstance(submitted, (str, bytes)) or not isinstance(submitted, Collection):
            raise ValueError("submitted must be a collection")
        submitted_items = tuple(submitted)
        if any(not isinstance(item, DraftItem) for item in submitted_items):
            raise ValueError("submitted items are invalid")
        selected = {item.family_id for item in submitted_items}
        if len(selected) != len(submitted_items):
            raise ValueError("submitted items contain a duplicate family")
        current_by_family = {item.family_id: item for item in draft.items}
        if any(current_by_family.get(item.family_id) != item for item in submitted_items):
            raise DraftConflict("review_draft_conflict")
        return self.replace_draft(
            draft,
            tuple(item for item in draft.items if item.family_id not in selected),
            now,
        )

    def put_review_batch(self, batch: CentralReviewBatch) -> CentralReviewBatch:
        if not isinstance(batch, CentralReviewBatch):
            raise TypeError("batch must be a CentralReviewBatch")
        record_json, record_digest = _canonical_record(batch.to_dict())
        with immediate(self.connection):
            row = self.connection.execute(
                """
                SELECT record_json, record_digest FROM web_review_batches
                WHERE organization_id = ? AND product_id = ? AND review_batch_id = ?
                """,
                (batch.organization_id, batch.product_id, batch.review_batch_id),
            ).fetchone()
            action = self.connection.execute(
                """
                SELECT record_json, record_digest FROM web_review_batches
                WHERE organization_id = ? AND actor_id = ? AND client_action_id = ?
                """,
                (batch.organization_id, batch.actor_id, batch.client_action_id),
            ).fetchone()
            for existing in (row, action):
                if existing is not None:
                    loaded = _read_record(
                        existing["record_json"], existing["record_digest"],
                        CentralReviewBatch, "review_batch",
                    )
                    if loaded == batch:
                        return loaded
                    raise WebRecordConflict("review_batch_conflict")
            self.connection.execute(
                """
                INSERT INTO web_review_batches(
                    organization_id, product_id, review_batch_id, actor_id,
                    client_action_id, request_digest, record_json, record_digest,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.organization_id, batch.product_id, batch.review_batch_id,
                    batch.actor_id, batch.client_action_id, batch.request_digest,
                    record_json, record_digest, batch.created_at,
                ),
            )
            for order, item in enumerate(batch.items):
                content_json: str | None = None
                content_digest: str | None = None
                if item.effective_content is not None:
                    content_json, content_digest = _canonical_record(
                        item.effective_content.to_dict()
                    )
                self.connection.execute(
                    """
                    INSERT INTO web_review_items(
                        organization_id, product_id, review_batch_id, item_order,
                        review_id, family_id, publication_candidate_id, repository_id,
                        revision_id, revision, content_digest, action,
                        effective_content_json, effective_content_digest, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.organization_id, batch.product_id, batch.review_batch_id,
                        order, item.review_id, item.family_id,
                        item.publication_candidate_id, item.repository_id,
                        item.revision_id, item.revision, item.content_digest,
                        item.action, content_json, content_digest, item.note,
                    ),
                )
        return batch

    def get_review_batch(
        self, organization_id: str, product_id: str, review_batch_id: str
    ) -> CentralReviewBatch | None:
        organization = require_id(organization_id, "organization_id")
        row = self.connection.execute(
            """
            SELECT record_json, record_digest FROM web_review_batches
            WHERE organization_id = ? AND product_id = ? AND review_batch_id = ?
            """,
            (organization, product_id, review_batch_id),
        ).fetchone()
        if row is None:
            return None
        batch = _read_record(
            row["record_json"], row["record_digest"], CentralReviewBatch, "review_batch"
        )
        if (
            batch.organization_id != organization
            or batch.product_id != product_id
            or batch.review_batch_id != review_batch_id
        ):
            raise WebRecordCorrupt("review_batch")
        return batch

    def put_preview(
        self, organization_id: str, product_id: str, record: PublicationRecord
    ) -> PublicationRecord:
        organization = require_id(organization_id, "organization_id")
        if not isinstance(record, PublicationRecord):
            raise TypeError("record must be a PublicationRecord")
        if record.product_id != product_id or record.state != "previewed":
            raise ValueError("preview product_id is invalid")
        record_json, record_digest = _canonical_record(record.to_dict())
        with immediate(self.connection):
            review = self.get_review_batch(
                organization, product_id, record.review_batch_id
            )
            if review is None:
                raise WebRecordConflict("publication_review_missing")
            row = self.connection.execute(
                """
                SELECT record_json, record_digest FROM web_publication_previews
                WHERE organization_id = ? AND product_id = ? AND preview_id = ?
                """,
                (organization, product_id, record.preview_id),
            ).fetchone()
            if row is not None:
                existing = _read_record(
                    row["record_json"], row["record_digest"], PublicationRecord,
                    "publication_preview",
                )
                if existing == record:
                    return existing
                raise WebRecordConflict("publication_preview_conflict")
            self.connection.execute(
                """
                INSERT INTO web_publication_previews(
                    organization_id, product_id, preview_id, review_batch_id,
                    actor_id, record_json, record_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    organization, product_id, record.preview_id,
                    record.review_batch_id, review.actor_id, record_json, record_digest,
                    record.created_at,
                ),
            )
        return record

    def get_preview(
        self, organization_id: str, preview_id: str
    ) -> PublicationRecord | None:
        organization = require_id(organization_id, "organization_id")
        row = self.connection.execute(
            """
            SELECT product_id, record_json, record_digest
            FROM web_publication_previews
            WHERE organization_id = ? AND preview_id = ?
            """,
            (organization, preview_id),
        ).fetchone()
        if row is None:
            return None
        record = _read_record(
            row["record_json"], row["record_digest"], PublicationRecord,
            "publication_preview",
        )
        if record.preview_id != preview_id or record.product_id != row["product_id"]:
            raise WebRecordCorrupt("publication_preview")
        return record

    def put_publication(self, publication: CentralPublication) -> CentralPublication:
        if not isinstance(publication, CentralPublication):
            raise TypeError("publication must be a CentralPublication")
        record_json, record_digest = _canonical_record(publication.to_dict())
        with immediate(self.connection):
            row = self.connection.execute(
                """
                SELECT record_json, record_digest FROM web_publications
                WHERE organization_id = ? AND product_id = ? AND publication_id = ?
                """,
                (publication.organization_id, publication.product_id, publication.publication_id),
            ).fetchone()
            if row is not None:
                existing = _read_record(
                    row["record_json"], row["record_digest"], CentralPublication,
                    "publication",
                )
                if existing == publication:
                    return existing
                raise WebRecordConflict("publication_conflict")
            try:
                self.connection.execute(
                    """
                    INSERT INTO web_publications(
                        organization_id, product_id, publication_id, preview_id,
                        actor_id, state, recovery_code, commit_sha, record_json,
                        record_digest, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        publication.organization_id, publication.product_id,
                        publication.publication_id, publication.preview_id,
                        publication.actor_id, publication.state,
                        publication.recovery_code, publication.commit_sha,
                        record_json, record_digest, publication.created_at,
                        publication.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise WebRecordConflict("publication_conflict") from error
        return publication

    def get_publication_by_preview(
        self, organization_id: str, preview_id: str
    ) -> CentralPublication | None:
        organization = require_id(organization_id, "organization_id")
        row = self.connection.execute(
            """
            SELECT record_json, record_digest FROM web_publications
            WHERE organization_id = ? AND preview_id = ?
            """,
            (organization, preview_id),
        ).fetchone()
        if row is None:
            return None
        return _read_record(
            row["record_json"], row["record_digest"], CentralPublication, "publication"
        )

    def claim_publication_families(
        self, publication: CentralPublication, family_ids: Collection[str]
    ) -> None:
        if not isinstance(publication, CentralPublication):
            raise TypeError("publication must be a CentralPublication")
        if isinstance(family_ids, (str, bytes)) or not isinstance(family_ids, Collection):
            raise ValueError("family_ids must be a collection")
        families = tuple(family_ids)
        if not families or len(set(families)) != len(families):
            raise ValueError("family_ids are invalid")
        for family_id in families:
            publication_candidate_id(family_id)
        with immediate(self.connection):
            for family_id in families:
                row = self.connection.execute(
                    """
                    SELECT publication_id FROM web_publication_families
                    WHERE organization_id = ? AND product_id = ? AND family_id = ?
                    """,
                    (publication.organization_id, publication.product_id, family_id),
                ).fetchone()
                if row is not None:
                    if row["publication_id"] == publication.publication_id:
                        continue
                    raise WebRecordConflict("publication_family_conflict")
                self.connection.execute(
                    """
                    INSERT INTO web_publication_families(
                        organization_id, product_id, family_id, publication_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        publication.organization_id, publication.product_id,
                        family_id, publication.publication_id,
                    ),
                )

    def replace_publication(
        self, expected: CentralPublication, replacement: CentralPublication
    ) -> CentralPublication:
        if not isinstance(expected, CentralPublication) or not isinstance(replacement, CentralPublication):
            raise TypeError("publication replacement requires CentralPublication values")
        if (
            expected.publication_id != replacement.publication_id
            or expected.organization_id != replacement.organization_id
            or expected.product_id != replacement.product_id
            or expected.preview_id != replacement.preview_id
        ):
            raise WebRecordConflict("publication_identity_conflict")
        exact_replay = replacement == expected
        mutable = frozenset(("state", "commit_sha", "recovery_code", "updated_at"))
        if not exact_replay and (
            {key: value for key, value in expected.to_dict().items() if key not in mutable}
            != {key: value for key, value in replacement.to_dict().items() if key not in mutable}
        ):
            raise WebRecordConflict("publication_immutable_fields")
        if not exact_replay and (expected.state, replacement.state) not in {
            ("confirmed", "committed_pending_push"),
            ("committed_pending_push", "completed"),
        }:
            raise WebRecordConflict("publication_state_conflict")
        record_json, record_digest = _canonical_record(replacement.to_dict())
        with immediate(self.connection):
            row = self.connection.execute(
                """
                SELECT record_json, record_digest FROM web_publications
                WHERE organization_id = ? AND product_id = ? AND publication_id = ?
                """,
                (expected.organization_id, expected.product_id, expected.publication_id),
            ).fetchone()
            if row is None:
                raise WebRecordConflict("publication_missing")
            current = _read_record(
                row["record_json"], row["record_digest"], CentralPublication, "publication"
            )
            if current == replacement:
                return replacement
            if current != expected:
                raise WebRecordConflict("publication_changed")
            self.connection.execute(
                """
                UPDATE web_publications
                SET state = ?, recovery_code = ?, commit_sha = ?, record_json = ?,
                    record_digest = ?, updated_at = ?
                WHERE organization_id = ? AND product_id = ? AND publication_id = ?
                """,
                (
                    replacement.state, replacement.recovery_code,
                    replacement.commit_sha, record_json, record_digest,
                    replacement.updated_at, replacement.organization_id,
                    replacement.product_id, replacement.publication_id,
                ),
            )
        return replacement

    def put_family_receipts(
        self,
        publication: CentralPublication,
        preview: PublicationRecord,
        commit_sha: str,
    ) -> None:
        if not isinstance(publication, CentralPublication) or not isinstance(preview, PublicationRecord):
            raise TypeError("publication and preview must be validated records")
        if publication.preview_id != preview.preview_id or publication.product_id != preview.product_id:
            raise WebRecordConflict("publication_preview_conflict")
        with immediate(self.connection):
            stored_publication = self.get_publication_by_preview(
                publication.organization_id, publication.preview_id
            )
            stored_preview = self.get_preview(
                publication.organization_id, preview.preview_id
            )
            if stored_publication != publication or stored_preview != preview:
                raise WebRecordConflict("publication_preview_conflict")
            review = self.get_review_batch(
                publication.organization_id, preview.product_id, preview.review_batch_id
            )
            if review is None:
                raise WebRecordConflict("publication_review_missing")
            families = {
                item.publication_candidate_id: item.family_id for item in review.items
            }
            for candidate_id, decision_id in zip(
                preview.candidate_ids, preview.decision_ids, strict=True
            ):
                family_id = families.get(candidate_id)
                if family_id is None:
                    raise WebRecordConflict("publication_family_missing")
                receipt = CandidatePublicationReceipt(
                    candidate_id=candidate_id,
                    decision_id=decision_id,
                    product_id=preview.product_id,
                    preview_id=preview.preview_id,
                    commit_sha=commit_sha,
                    recorded_at=publication.updated_at,
                )
                row = self.connection.execute(
                    """
                    SELECT publication_candidate_id, decision_id, preview_id,
                           commit_sha, recorded_at
                    FROM web_candidate_receipts
                    WHERE organization_id = ? AND product_id = ? AND family_id = ?
                    """,
                    (publication.organization_id, preview.product_id, family_id),
                ).fetchone()
                expected = (
                    receipt.candidate_id, receipt.decision_id, receipt.preview_id,
                    receipt.commit_sha, receipt.recorded_at,
                )
                if row is not None:
                    actual = tuple(row[key] for key in (
                        "publication_candidate_id", "decision_id", "preview_id",
                        "commit_sha", "recorded_at",
                    ))
                    if actual == expected:
                        continue
                    raise WebRecordConflict("candidate_receipt_conflict")
                self.connection.execute(
                    """
                    INSERT INTO web_candidate_receipts(
                        organization_id, product_id, family_id,
                        publication_candidate_id, decision_id, preview_id,
                        commit_sha, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        publication.organization_id, preview.product_id, family_id,
                        receipt.candidate_id, receipt.decision_id, receipt.preview_id,
                        receipt.commit_sha, receipt.recorded_at,
                    ),
                )

    def action_result(
        self, organization_id: str, actor_id: str, action_kind: ActionKind,
        client_action_id: str,
    ) -> ActionResult | None:
        organization = require_id(organization_id, "organization_id")
        actor = require_id(actor_id, "actor_id")
        row = self.connection.execute(
            """
            SELECT organization_id, actor_id, action_kind, client_action_id,
                   request_digest, result_id, created_at
            FROM web_action_results
            WHERE organization_id = ? AND actor_id = ? AND action_kind = ?
              AND client_action_id = ?
            """,
            (organization, actor, action_kind, client_action_id),
        ).fetchone()
        if row is None:
            return None
        return ActionResult.from_dict(dict(row))

    def record_action(
        self, organization_id: str, actor_id: str, action_kind: ActionKind,
        client_action_id: str, request_digest: str, result_id: str, now: str,
    ) -> str:
        result = ActionResult(
            organization_id, actor_id, action_kind, client_action_id,
            request_digest, result_id, now,
        )
        with immediate(self.connection):
            existing = self.action_result(
                result.organization_id, result.actor_id, result.action_kind,
                result.client_action_id,
            )
            if existing is not None:
                if (
                    existing.request_digest == result.request_digest
                    and existing.result_id == result.result_id
                ):
                    return existing.result_id
                raise WebActionConflict("web_action_conflict")
            self.connection.execute(
                """
                INSERT INTO web_action_results(
                    organization_id, actor_id, action_kind, client_action_id,
                    request_digest, result_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.organization_id, result.actor_id, result.action_kind,
                    result.client_action_id, result.request_digest,
                    result.result_id, result.created_at,
                ),
            )
        return result.result_id
