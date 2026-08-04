"""Product-owned SQLite schema for central Decision Web persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import CandidateBatchUpload


WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_review_drafts (
  organization_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version >= 0),
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, actor_id, product_id)
);

CREATE TABLE IF NOT EXISTS web_candidate_revision_batches (
  organization_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, repository_id, family_id, revision_id, request_id),
  FOREIGN KEY(request_id) REFERENCES capture_requests(request_id)
);

CREATE TABLE IF NOT EXISTS web_review_batches (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  client_action_id TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  submission_order INTEGER CHECK(submission_order > 0),
  PRIMARY KEY(organization_id, product_id, review_batch_id),
  UNIQUE(organization_id, actor_id, client_action_id)
);

CREATE TABLE IF NOT EXISTS web_review_items (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  item_order INTEGER NOT NULL CHECK(item_order >= 0),
  review_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  publication_candidate_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision > 0),
  content_digest TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('accept','edit_accept','reject','skip')),
  effective_content_json TEXT,
  effective_content_digest TEXT,
  note TEXT,
  PRIMARY KEY(organization_id, product_id, review_batch_id, item_order),
  UNIQUE(organization_id, product_id, review_id),
  FOREIGN KEY(organization_id, product_id, review_batch_id)
    REFERENCES web_review_batches(organization_id, product_id, review_batch_id)
);

CREATE TABLE IF NOT EXISTS web_review_submission_results (
  organization_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  PRIMARY KEY(organization_id, actor_id, review_batch_id),
  FOREIGN KEY(organization_id, product_id, review_batch_id)
    REFERENCES web_review_batches(organization_id, product_id, review_batch_id)
);

CREATE TABLE IF NOT EXISTS web_action_results (
  organization_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  action_kind TEXT NOT NULL CHECK(action_kind IN ('review','preview','publish','resume')),
  client_action_id TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  result_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, actor_id, action_kind, client_action_id)
);

CREATE TABLE IF NOT EXISTS web_publication_previews (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  preview_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, preview_id),
  FOREIGN KEY(organization_id, product_id, review_batch_id)
    REFERENCES web_review_batches(organization_id, product_id, review_batch_id)
);

CREATE TABLE IF NOT EXISTS web_publications (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  publication_id TEXT NOT NULL,
  preview_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('confirmed','committed_pending_push','completed')),
  recovery_code TEXT,
  commit_sha TEXT,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, publication_id),
  UNIQUE(organization_id, product_id, preview_id),
  FOREIGN KEY(organization_id, product_id, preview_id)
    REFERENCES web_publication_previews(organization_id, product_id, preview_id)
);

CREATE TABLE IF NOT EXISTS web_publication_families (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  publication_id TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, family_id),
  FOREIGN KEY(organization_id, product_id, publication_id)
    REFERENCES web_publications(organization_id, product_id, publication_id)
);

CREATE TABLE IF NOT EXISTS web_candidate_receipts (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  publication_candidate_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  preview_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, family_id),
  UNIQUE(organization_id, product_id, decision_id)
);

CREATE INDEX IF NOT EXISTS web_candidate_revision_batches_filter
ON web_candidate_revision_batches(organization_id, request_id, revision_id);

CREATE INDEX IF NOT EXISTS web_review_batches_history
ON web_review_batches(organization_id, product_id, created_at, review_batch_id);

CREATE INDEX IF NOT EXISTS web_review_items_family
ON web_review_items(organization_id, product_id, family_id, review_batch_id);

CREATE INDEX IF NOT EXISTS web_publications_history
ON web_publications(organization_id, product_id, created_at);

CREATE INDEX IF NOT EXISTS web_candidate_receipts_decision
ON web_candidate_receipts(organization_id, product_id, decision_id);
"""


class CentralCandidateStateCorrupt(ValueError):
    def __init__(self) -> None:
        super().__init__("central_candidate_state_corrupt")


def initialize_web_schema(connection: sqlite3.Connection) -> None:
    """Create Web tables and recover their immutable Candidate associations."""

    connection.executescript(WEB_SCHEMA)
    _ensure_review_submission_order(connection)
    _backfill_candidate_revision_batches(connection)


def _ensure_review_submission_order(connection: sqlite3.Connection) -> None:
    """Migrate and freeze one durable order for every immutable Review batch."""

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(web_review_batches)"
        ).fetchall()
    }
    if "submission_order" not in columns:
        connection.execute(
            "ALTER TABLE web_review_batches ADD COLUMN submission_order INTEGER"
        )
    maxima = {
        (row["organization_id"], row["product_id"]): int(row["maximum"])
        for row in connection.execute(
            """
            SELECT organization_id, product_id,
                   COALESCE(MAX(submission_order), 0) AS maximum
            FROM web_review_batches
            GROUP BY organization_id, product_id
            """
        ).fetchall()
    }
    rows = connection.execute(
        """
        SELECT rowid, organization_id, product_id
        FROM web_review_batches
        WHERE submission_order IS NULL
        ORDER BY organization_id, product_id, rowid
        """
    ).fetchall()
    for row in rows:
        key = (row["organization_id"], row["product_id"])
        order = maxima.get(key, 0) + 1
        connection.execute(
            "UPDATE web_review_batches SET submission_order = ? WHERE rowid = ?",
            (order, row["rowid"]),
        )
        maxima[key] = order
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS web_review_batches_submission_order
        ON web_review_batches(organization_id, product_id, submission_order)
        """
    )


def record_candidate_revision_batch(
    connection: sqlite3.Connection,
    organization_id: str,
    repository_id: str,
    request_id: str,
    revision: object,
    observed_at: str,
) -> None:
    """Bind one accepted immutable revision to its Capture request."""

    connection.execute(
        """
        INSERT OR IGNORE INTO web_candidate_revision_batches(
            organization_id, repository_id, family_id, revision_id,
            request_id, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            organization_id,
            repository_id,
            revision.family_id,
            revision.revision_id,
            request_id,
            observed_at,
        ),
    )


def _backfill_candidate_revision_batches(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT request_id, organization_id, repository_id, batch_json,
               batch_record_digest, acknowledged_at
        FROM candidate_batches
        ORDER BY request_id
        """
    ).fetchall()
    for row in rows:
        try:
            encoded = row["batch_json"].encode("utf-8")
            value = json.loads(row["batch_json"])
            batch = CandidateBatchUpload.from_dict(value)
            canonical = canonical_json_bytes(batch.to_dict())
            if (
                encoded != canonical
                or hashlib.sha256(canonical).hexdigest() != row["batch_record_digest"]
                or batch.request_id != row["request_id"]
                or batch.repository_id != row["repository_id"]
            ):
                raise ValueError
        except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise CentralCandidateStateCorrupt() from None
        for item in batch.items:
            record_candidate_revision_batch(
                connection,
                row["organization_id"],
                row["repository_id"],
                row["request_id"],
                item,
                row["acknowledged_at"],
            )
