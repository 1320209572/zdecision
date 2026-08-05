"""Decision-space-owned SQLite schema for central Decision Web persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import CandidateBatchUpload
from zdecision.central.web.contracts import (
    CentralPublication,
    CentralReviewBatch,
    ReviewDraft,
    ReviewSubmissionSnapshot,
)


WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_review_drafts (
  organization_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version >= 0),
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, actor_id, decision_space_id)
);

CREATE TABLE IF NOT EXISTS web_candidate_revision_batches (
  organization_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  decision_space_id TEXT,
  ownership_json TEXT,
  observed_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, repository_id, family_id, revision_id, request_id)
);

CREATE TABLE IF NOT EXISTS web_review_batches (
  organization_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  compatibility_product_id TEXT NOT NULL,
  compatibility_product_name TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  client_action_id TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  submission_order INTEGER CHECK(submission_order > 0),
  PRIMARY KEY(organization_id, decision_space_id, review_batch_id),
  UNIQUE(organization_id, actor_id, client_action_id)
);

CREATE TABLE IF NOT EXISTS web_review_items (
  organization_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
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
  PRIMARY KEY(organization_id, decision_space_id, review_batch_id, item_order),
  UNIQUE(organization_id, decision_space_id, review_id),
  FOREIGN KEY(organization_id, decision_space_id, review_batch_id)
    REFERENCES web_review_batches(organization_id, decision_space_id, review_batch_id)
);

CREATE TABLE IF NOT EXISTS web_review_submission_results (
  organization_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  PRIMARY KEY(organization_id, actor_id, review_batch_id),
  FOREIGN KEY(organization_id, decision_space_id, review_batch_id)
    REFERENCES web_review_batches(organization_id, decision_space_id, review_batch_id)
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
  decision_space_id TEXT NOT NULL,
  compatibility_product_id TEXT NOT NULL,
  preview_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, decision_space_id, preview_id),
  FOREIGN KEY(organization_id, decision_space_id, review_batch_id)
    REFERENCES web_review_batches(organization_id, decision_space_id, review_batch_id)
);

CREATE TABLE IF NOT EXISTS web_publications (
  organization_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  compatibility_product_id TEXT NOT NULL,
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
  PRIMARY KEY(organization_id, decision_space_id, publication_id),
  UNIQUE(organization_id, decision_space_id, preview_id),
  FOREIGN KEY(organization_id, decision_space_id, preview_id)
    REFERENCES web_publication_previews(organization_id, decision_space_id, preview_id)
);

CREATE TABLE IF NOT EXISTS web_publication_families (
  organization_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  publication_id TEXT NOT NULL,
  PRIMARY KEY(organization_id, decision_space_id, family_id),
  FOREIGN KEY(organization_id, decision_space_id, publication_id)
    REFERENCES web_publications(organization_id, decision_space_id, publication_id)
);

CREATE TABLE IF NOT EXISTS web_candidate_receipts (
  organization_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  compatibility_product_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  publication_candidate_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  preview_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, decision_space_id, family_id),
  UNIQUE(organization_id, decision_space_id, decision_id)
);

CREATE INDEX IF NOT EXISTS web_candidate_revision_batches_filter
ON web_candidate_revision_batches(organization_id, request_id, revision_id);

CREATE INDEX IF NOT EXISTS web_review_batches_history
ON web_review_batches(organization_id, decision_space_id, created_at, review_batch_id);

CREATE INDEX IF NOT EXISTS web_review_items_family
ON web_review_items(organization_id, decision_space_id, family_id, review_batch_id);

CREATE INDEX IF NOT EXISTS web_publications_history
ON web_publications(organization_id, decision_space_id, created_at);

CREATE INDEX IF NOT EXISTS web_candidate_receipts_decision
ON web_candidate_receipts(organization_id, decision_space_id, decision_id);
"""


class CentralCandidateStateCorrupt(ValueError):
    def __init__(self) -> None:
        super().__init__("central_candidate_state_corrupt")


def initialize_web_schema(connection: sqlite3.Connection) -> None:
    """Create Web tables and recover their immutable Candidate associations."""

    _migrate_leaf_owned_web_tables(connection)
    connection.executescript(WEB_SCHEMA)
    _ensure_candidate_ownership_columns(connection)
    _ensure_review_submission_order(connection)
    _backfill_candidate_revision_batches(connection)


def _migrate_leaf_owned_web_tables(connection: sqlite3.Connection) -> None:
    """Project the V1 product owner key onto its immutable leaf identity."""

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(web_review_drafts)"
        ).fetchall()
    }
    if not columns or "decision_space_id" in columns:
        return
    if "product_id" not in columns:
        raise CentralCandidateStateCorrupt()

    owner_tables = (
        "web_review_drafts",
        "web_review_batches",
        "web_review_items",
        "web_review_submission_results",
        "web_publication_previews",
        "web_publications",
        "web_publication_families",
        "web_candidate_receipts",
    )
    owns_transaction = not connection.in_transaction
    savepoint = "leaf_owned_web_migration"
    savepoint_started = False
    deferred_by_migration = False
    try:
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection.execute(f"SAVEPOINT {savepoint}")
            savepoint_started = True
        if (
            bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            and not bool(
                connection.execute("PRAGMA defer_foreign_keys").fetchone()[0]
            )
        ):
            connection.execute("PRAGMA defer_foreign_keys = ON")
            deferred_by_migration = True
        for table in owner_tables:
            connection.execute(
                f"ALTER TABLE {table} RENAME COLUMN product_id "
                "TO decision_space_id"
            )
        for table, additions in (
            (
                "web_review_batches",
                (
                    "compatibility_product_id TEXT NOT NULL DEFAULT ''",
                    "compatibility_product_name TEXT NOT NULL DEFAULT ''",
                ),
            ),
            (
                "web_publication_previews",
                ("compatibility_product_id TEXT NOT NULL DEFAULT ''",),
            ),
            (
                "web_publications",
                ("compatibility_product_id TEXT NOT NULL DEFAULT ''",),
            ),
            (
                "web_candidate_receipts",
                ("compatibility_product_id TEXT NOT NULL DEFAULT ''",),
            ),
        ):
            for addition in additions:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {addition}"
                )

        mappings: dict[tuple[str, str], str] = {}
        for row in connection.execute(
            """SELECT organization_id, decision_space_id,
                      compatibility_product_id
               FROM decision_spaces"""
        ).fetchall():
            key = (row["organization_id"], row["compatibility_product_id"])
            existing = mappings.get(key)
            if existing is not None and existing != row["decision_space_id"]:
                raise CentralCandidateStateCorrupt()
            mappings[key] = row["decision_space_id"]
        owners = {
            (row["organization_id"], row["decision_space_id"])
            for table in owner_tables
            for row in connection.execute(
                f"SELECT DISTINCT organization_id, decision_space_id "
                f"FROM {table}"
            ).fetchall()
        }
        if any(owner not in mappings for owner in owners):
            raise CentralCandidateStateCorrupt()

        def rewrite(
            table: str,
            record_type: type[object],
            transform,
        ) -> None:
            rows = connection.execute(
                f"SELECT rowid, organization_id, decision_space_id, "
                f"record_json FROM {table}"
            ).fetchall()
            for row in rows:
                product = row["decision_space_id"]
                space = mappings[(row["organization_id"], product)]
                raw = json.loads(row["record_json"])
                record = record_type.from_dict(transform(raw, space, product))
                encoded = canonical_json_bytes(record.to_dict())
                connection.execute(
                    f"UPDATE {table} SET decision_space_id = ?, "
                    "record_json = ?, record_digest = ? WHERE rowid = ?",
                    (
                        space,
                        encoded.decode("utf-8"),
                        hashlib.sha256(encoded).hexdigest(),
                        row["rowid"],
                    ),
                )

        rewrite(
            "web_review_drafts",
            ReviewDraft,
            lambda raw, space, _product: {
                **{key: value for key, value in raw.items() if key != "product_id"},
                "decision_space_id": space,
            },
        )
        rewrite(
            "web_review_batches",
            CentralReviewBatch,
            lambda raw, space, product: {
                **{
                    key: value
                    for key, value in raw.items()
                    if key not in ("product_id", "product_name")
                },
                "decision_space_id": space,
                "compatibility_product_id": product,
                "compatibility_product_name": raw["product_name"],
            },
        )
        rewrite(
            "web_review_submission_results",
            ReviewSubmissionSnapshot,
            lambda raw, space, _product: {
                **{key: value for key, value in raw.items() if key != "product_id"},
                "decision_space_id": space,
            },
        )
        rewrite(
            "web_publications",
            CentralPublication,
            lambda raw, space, product: {
                **{key: value for key, value in raw.items() if key != "product_id"},
                "decision_space_id": space,
                "compatibility_product_id": product,
            },
        )

        for table in owner_tables:
            connection.executemany(
                f"UPDATE {table} SET decision_space_id = ? "
                "WHERE organization_id = ? AND decision_space_id = ?",
                (
                    (space, organization, product)
                    for (organization, product), space in mappings.items()
                ),
            )
        connection.execute(
            """UPDATE web_review_batches
               SET compatibility_product_id = json_extract(
                       record_json, '$.compatibility_product_id'),
                   compatibility_product_name = json_extract(
                       record_json, '$.compatibility_product_name')"""
        )
        connection.execute(
            """UPDATE web_publication_previews
               SET compatibility_product_id = json_extract(
                       record_json, '$.product_id')"""
        )
        connection.execute(
            """UPDATE web_publications
               SET compatibility_product_id = json_extract(
                       record_json, '$.compatibility_product_id')"""
        )
        connection.execute(
            """UPDATE web_candidate_receipts
               SET compatibility_product_id = (
                   SELECT publication.compatibility_product_id
                   FROM web_publications AS publication
                   WHERE publication.organization_id =
                         web_candidate_receipts.organization_id
                     AND publication.decision_space_id =
                         web_candidate_receipts.decision_space_id
                     AND publication.preview_id =
                         web_candidate_receipts.preview_id
               )"""
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CentralCandidateStateCorrupt()
        if owns_transaction:
            connection.commit()
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            savepoint_started = False
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        if owns_transaction:
            connection.rollback()
        elif savepoint_started:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise CentralCandidateStateCorrupt() from None
    finally:
        if deferred_by_migration:
            connection.execute("PRAGMA defer_foreign_keys = OFF")


def _ensure_candidate_ownership_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(web_candidate_revision_batches)"
        ).fetchall()
    }
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(web_candidate_revision_batches)"
    ).fetchall()
    if any(row["table"] == "capture_requests" for row in foreign_keys):
        decision_projection = (
            "decision_space_id" if "decision_space_id" in columns else "NULL"
        )
        ownership_projection = (
            "ownership_json" if "ownership_json" in columns else "NULL"
        )
        connection.executescript(
            f"""
            CREATE TABLE web_candidate_revision_batches_v2 (
              organization_id TEXT NOT NULL,
              repository_id TEXT NOT NULL,
              family_id TEXT NOT NULL,
              revision_id TEXT NOT NULL,
              request_id TEXT NOT NULL,
              decision_space_id TEXT,
              ownership_json TEXT,
              observed_at TEXT NOT NULL,
              PRIMARY KEY(
                organization_id, repository_id, family_id, revision_id, request_id
              )
            );
            INSERT INTO web_candidate_revision_batches_v2(
              organization_id, repository_id, family_id, revision_id,
              request_id, decision_space_id, ownership_json, observed_at
            )
            SELECT organization_id, repository_id, family_id, revision_id,
              request_id, {decision_projection}, {ownership_projection}, observed_at
            FROM web_candidate_revision_batches;
            DROP TABLE web_candidate_revision_batches;
            ALTER TABLE web_candidate_revision_batches_v2
              RENAME TO web_candidate_revision_batches;
            CREATE INDEX IF NOT EXISTS web_candidate_revision_batches_filter
            ON web_candidate_revision_batches(
              organization_id, request_id, revision_id
            );
            """
        )
        columns = {"organization_id", "repository_id", "family_id", "revision_id", "request_id", "decision_space_id", "ownership_json", "observed_at"}
    if "decision_space_id" not in columns:
        connection.execute(
            "ALTER TABLE web_candidate_revision_batches ADD COLUMN decision_space_id TEXT"
        )
    if "ownership_json" not in columns:
        connection.execute(
            "ALTER TABLE web_candidate_revision_batches ADD COLUMN ownership_json TEXT"
        )


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
        (row["organization_id"], row["decision_space_id"]): int(row["maximum"])
        for row in connection.execute(
            """
            SELECT organization_id, decision_space_id,
                   COALESCE(MAX(submission_order), 0) AS maximum
            FROM web_review_batches
            GROUP BY organization_id, decision_space_id
            """
        ).fetchall()
    }
    rows = connection.execute(
        """
        SELECT rowid, organization_id, decision_space_id
        FROM web_review_batches
        WHERE submission_order IS NULL
        ORDER BY organization_id, decision_space_id, rowid
        """
    ).fetchall()
    for row in rows:
        key = (row["organization_id"], row["decision_space_id"])
        order = maxima.get(key, 0) + 1
        connection.execute(
            "UPDATE web_review_batches SET submission_order = ? WHERE rowid = ?",
            (order, row["rowid"]),
        )
        maxima[key] = order
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS web_review_batches_submission_order
        ON web_review_batches(organization_id, decision_space_id, submission_order)
        """
    )


def record_candidate_revision_batch(
    connection: sqlite3.Connection,
    organization_id: str,
    repository_id: str,
    request_id: str,
    revision: object,
    observed_at: str,
    ownership: object | None = None,
) -> None:
    """Bind one accepted immutable revision to its Capture request."""

    connection.execute(
        """
        INSERT INTO web_candidate_revision_batches(
            organization_id, repository_id, family_id, revision_id,
            request_id, decision_space_id, ownership_json, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(organization_id, repository_id, family_id, revision_id, request_id)
        DO UPDATE SET
            decision_space_id = COALESCE(excluded.decision_space_id, decision_space_id),
            ownership_json = COALESCE(excluded.ownership_json, ownership_json)
        """,
        (
            organization_id,
            repository_id,
            revision.family_id,
            revision.revision_id,
            request_id,
            None if ownership is None else ownership.decision_space_id,
            None if ownership is None else canonical_json_bytes(ownership.to_dict()).decode("utf-8"),
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
