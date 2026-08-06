# Trusted Recall Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Gate 2's authenticated, Ed25519-signed, complete Decision distribution path from Central's verified Registry projection to an atomically activated local trusted-data cache, including clean-device onboarding, generic content-addressed artifact acquisition, rollback/freeze protection, clock safety, last-known-good behavior, and complete-set V1 removal semantics.

**Architecture:** Central derives one organization-wide immutable generation only from `RegistryProjectionStore.load_active()`, signs its canonical manifest with an Ed25519 private key, and serves canonical catalog, leaf snapshot, retrieval-profile, and content-addressed artifact bytes behind the existing device-authenticated API. The persistent Agent service verifies the configured public trust root, advances a durable manifest high-water mark before downloading, stages every referenced byte, and changes one SQLite trusted-generation pointer only after the whole signed data generation validates. Gate 3 consumes the immutable `TrustedRecallBundle`, selects the production model/runtime, builds and validates complete indexes, and owns the later recall-ready pointer; no Prompt, PRD, source, path, vector, candidate, or score enters this distribution API.

**Tech Stack:** Python 3.11, `unittest`, FastAPI, HTTPX streaming, SQLite/WAL, the repository's `canonical_json_bytes`, SHA-256, and `cryptography` Ed25519.

## Global Constraints

- Registry V1 is the only current producer. It emits revision `1` and lifecycle `active`; this plan does not alter `src/zdecision/registry/models.py`, loosen `registry_decision_projection.lifecycle`, add Registry history, or claim Registry V2 behavior.
- Each distributed document retains its canonical V1 `scope_summary`. Gate 4 derives `display_title = scope_summary`; this plan does not add a `display_title` Registry or distribution field.
- Under the approved V1 interpretation, an entry that was present in the prior complete signed active-head set and is absent from the newly recall-ready complete signed active-head set loses authority immediately with local reason `removed_from_active_heads`. Central does not synthesize or accumulate invalidation records; the two complete sets are sufficient.
- A future local transition type may add an ordinary-revision branch. The V1 publisher emits only r1 active heads, and no Gate 2 acceptance result may claim that a revision, retirement, supersession, or invalidation record was produced by Registry V1.
- Manifest signing is Ed25519 over `canonical_json_bytes(manifest.to_dict())`. The signature is outside the signed object. SHA-256 remains the digest for manifests, catalog payloads, leaf snapshots, Decision documents, profiles, and generic artifacts. Gate 3 adds index digests.
- The Agent accepts only an owner-readable configured trust root. Central alone receives the private key. Unknown keys fail closed; there is no trust-on-first-use path.
- Central constructs recall state only from `RegistryProjectionStore.load_active(organization_id)`. Recall requests never read Git and never read Candidate, Review, preview, or private Publication tables.
- A Central projection in `syncing` or `unavailable` may serve the previous unchanged signed manifest, but cannot mint a generation or extend a lease. A completed Publication can advance recall only after the active projection reports that exact commit and tree.
- A content change, catalog change, or retrieval-profile change mints a higher generation. A code-only commit with the same Registry tree does not. Lease renewal also mints a higher generation so a changed `issued_at` or `expires_at` can never create a different digest at the same generation.
- One generation covers every enabled leaf. An unchanged leaf reuses its prior `decision_version` and snapshot digest, but the Gate 2 trusted-data pointer advances only when the catalog, every enabled leaf, profile, and every referenced artifact are verified. This pointer is not a recall-ready or indexed-generation pointer.
- The high-water mark advances after signature, organization, structure, and signed-time validation, but before referenced downloads. A failed download or staging operation leaves the old trusted-data pointer intact; retrying the same generation and same digest resumes safely.
- Agent Prompt handling performs local reads only. Startup/onboarding and the persistent service perform network synchronization in the background according to the signed `refresh_interval_seconds` policy.
- Gate 2 uses a deterministic fake four-artifact pack to prove real download, digest validation, staging, and atomic trusted-data activation from an empty device cache. It does not load tokenizers or models, choose a production model, add a runtime dependency, build an index, run a retrieval smoke query, or claim recall readiness. Those steps and BM25/dense/path/reranker quality belong to Gate 3.
- The signed `RetrievalProfileManifest` represents exactly one selected embedding/reranker pair and their two tokenizers. Gate 3 candidate configurations remain private benchmark input; only its winner may later be published through this unchanged contract.
- Final output caps stay Gate 3 code constants: at most 8 complete Decisions and 10,000 injected bytes. They are not profile-controlled distribution fields.
- Every commit command in this plan stages exact paths. Preserve the pre-existing specification edit and any other user-owned worktree changes.

---

## Stable Interface Handoff

The following names are the contract between this plan and the Gate 3 retrieval plan. Do not rename them independently.

| File | Public interface | Responsibility |
|---|---|---|
| `src/zdecision/recall/contracts.py` | `DistributedDecisionRevision`, `LeafDecisionSnapshot`, `RecallCatalog`, `RetrievalArtifact`, `RetrievalProfileManifest`, `LeafManifestEntry`, `OrganizationManifest`, `SignedOrganizationManifest` | Strict signed-distribution wire values and digest/count validation; no Session or intent state |
| `src/zdecision/recall/signing.py` | `RecallTrustRoot`, `RecallSigningKey`, `VerifiedManifest`, `sign_manifest()`, `verify_manifest()` | Raw 32-byte Ed25519 key loading and verification |
| `src/zdecision/central/recall_distribution.py` | `RecallProfilePack`, `RecallDistributionStore`, `RecallDistributionPublisher`, `RecallDistributionService` | Projection-to-generation construction and immutable blob reads |
| `src/zdecision/agent/recall_clock.py` | `RecallLeaseClock`, `LeaseAssessment` | Signed-time, monotonic-deadline, restart, and rollback decisions |
| `src/zdecision/agent/recall_cache.py` | `ArtifactBinding`, `TrustedLeafSnapshot`, `TrustedDecisionRevision`, `TrustedRecallBundle`, `RecallReadiness`, `RecallCacheStore` | Durable staging, trusted-data activation, LKG/freshness reads, and the Gate 3 corpus handoff |
| `src/zdecision/agent/recall_artifacts.py` | `RecallArtifactDownloader` | Bounded streaming, hashing, fsync, and content-addressed installation |
| `src/zdecision/agent/recall_sync.py` | `RecallSynchronizer`, `RecallSyncResult` | Authenticated fetch, verify, stage, prepare, activate, and schedule |

`TrustedRecallBundle` is the only Gate 2 corpus handoff to Gate 3. It contains the complete full canonical active-head set, the signed profile, and immutable local artifact paths, but no indexes and no claim that recall can run. It is returned only inside the local Agent process; Central never receives the query that Gate 3 will later rank.

```python
@dataclass(frozen=True)
class TrustedRecallBundle:
    organization_id: str
    generation: int
    manifest_digest: str
    registry_tree_oid: str
    catalog_version: str
    retrieval_profile_digest: str
    retrieval_profile: RetrievalProfileManifest
    expires_at: str
    freshness: Literal["current", "degraded"]
    artifacts: tuple[ArtifactBinding, ...]
    leaves: tuple[TrustedLeafSnapshot, ...]
    decisions: tuple[TrustedDecisionRevision, ...]
```

---

### Task 1: Freeze canonical recall contracts and Ed25519 trust

**Files:**

- Modify: `src/zdecision/recall/__init__.py`
- Create: `src/zdecision/recall/contracts.py`
- Create: `src/zdecision/recall/signing.py`
- Modify: `pyproject.toml`
- Create: `tests/test_recall_contracts.py`

- [ ] **Step 1: Write failing contract, canonicalization, and signature tests**

Cover strict field sets, deterministic sort order, duplicate rejection, count/digest recomputation, malformed timestamps, generation bounds, profile resource bounds, canonical round trips, correct-key verification, wrong-key rejection, unknown-key rejection, signature tampering, manifest tampering, and raw-key length validation.

The fixture profile must exercise every signed retrieval control that Gate 3 consumes:

```python
embedding_model = RetrievalArtifact(
    role="embedding_model", digest="1" * 64, size_bytes=23,
    media_type="application/octet-stream",
)
embedding_tokenizer = RetrievalArtifact(
    role="embedding_tokenizer", digest="2" * 64, size_bytes=27,
    media_type="application/octet-stream",
)
reranker_model = RetrievalArtifact(
    role="reranker_model", digest="3" * 64, size_bytes=22,
    media_type="application/octet-stream",
)
reranker_tokenizer = RetrievalArtifact(
    role="reranker_tokenizer", digest="4" * 64, size_bytes=26,
    media_type="application/octet-stream",
)
profile = RetrievalProfileManifest(
    retrieval_profile_id="recall-fixture-v1",
    embedding_model_id="fixture-embedding",
    embedding_model_revision="1",
    embedding_model_digest="1" * 64,
    embedding_tokenizer_digest="2" * 64,
    embedding_dimension=16,
    embedding_max_tokens=256,
    reranker_model_id="fixture-reranker",
    reranker_model_revision="1",
    reranker_model_digest="3" * 64,
    reranker_tokenizer_digest="4" * 64,
    reranker_max_tokens=512,
    index_schema_version=1,
    bm25_candidate_depth=80,
    dense_candidate_depth=80,
    path_candidate_depth=40,
    union_candidate_depth=120,
    rerank_depth=40,
    reciprocal_rank_constant=60,
    bm25_weight=1.0,
    dense_weight=1.0,
    path_weight=1.25,
    reranker_threshold=0.2,
    bm25_k1=1.2,
    bm25_b=0.75,
    artifacts=(embedding_model, embedding_tokenizer, reranker_model, reranker_tokenizer),
    created_at="2026-08-06T00:00:00Z",
)
```

Run the RED test:

```bash
.venv/bin/python -m unittest tests.test_recall_contracts -v
```

Expected: FAIL because `zdecision.recall.contracts` and `zdecision.recall.signing` do not exist.

- [ ] **Step 2: Add the exact shared dataclasses and validation rules**

Implement strict `to_dict()` / `from_dict()` pairs. Lists on the wire become immutable tuples in Python. All SHA-256 strings are lowercase 64-character hex; timestamps are UTC `Z` values; generations and versions are positive non-boolean integers; all lists reject duplicate stable identities and must already be in canonical identity order when parsed.

Use these exact signed snapshot shapes. Do not add `RecallIntent`, Session, intent-epoch, host-state, injected-set, pinned-session, or synthesized invalidation records here; Gate 1 owns Session contracts in `src/zdecision/recall/session.py` and host state in `src/zdecision/agent/recall_host_state.py`.

```python
@dataclass(frozen=True)
class DistributedDecisionRevision:
    decision_id: str
    revision: int
    digest: str
    lifecycle: Literal["active"]
    document: Mapping[str, object]

@dataclass(frozen=True)
class LeafDecisionSnapshot:
    organization_id: str
    decision_space_id: str
    compatibility_product_id: str
    decision_version: int
    registry_tree_oid: str
    active_revisions: tuple[DistributedDecisionRevision, ...]
```

`LeafDecisionSnapshot` recomputes each embedded Decision digest from `canonical_json_bytes(revision.document)`, requires lifecycle `active`, and rejects duplicate `(decision_id, revision, digest)` identities. `active_manifest_digest` is the SHA-256 digest of the canonical sorted identity list; it is a property, not trusted input. The snapshot is the entire active set, so absence in a later complete snapshot is the signed removal fact.

Use these exact catalog and organization manifest shapes:

```python
@dataclass(frozen=True)
class RecallCatalog:
    organization_id: str
    catalog_version: str
    groups: tuple[CatalogGroup, ...]
    leaves: tuple[LeafDecisionSpace, ...]

@dataclass(frozen=True)
class LeafManifestEntry:
    decision_space_id: str
    compatibility_product_id: str
    decision_version: int
    snapshot_digest: str
    active_manifest_digest: str
    active_count: int

@dataclass(frozen=True)
class OrganizationManifest:
    schema_version: Literal[1]
    organization_id: str
    generation: int
    registry_tree_oid: str
    catalog_version: str
    leaves: tuple[LeafManifestEntry, ...]
    retrieval_profile_digest: str
    refresh_interval_seconds: int
    key_id: str
    issued_at: str
    expires_at: str

@dataclass(frozen=True)
class SignedOrganizationManifest:
    manifest: OrganizationManifest
    signature_ed25519_base64: str
```

`RecallCatalog.catalog_version` is the SHA-256 of canonical bytes for `{organization_id, groups, leaves}` without the version field, avoiding a self-referential digest. `OrganizationManifest.digest` is the SHA-256 of its complete canonical bytes. Restrict `refresh_interval_seconds` to `60..86400`, signed lease length to `1..86400` seconds, leaf count to `0..1000`, and artifact declared size to `1..4_294_967_296` bytes.

Define `RetrievalArtifact` as `(role, digest, size_bytes, media_type)` with roles `embedding_model`, `embedding_tokenizer`, `reranker_model`, and `reranker_tokenizer`, exactly once each. Define `RetrievalProfileManifest` with every field shown in the test fixture. Validate positive model token limits, `embedding_dimension <= 65536`, candidate depths `1..10000`, `rerank_depth <= union_candidate_depth`, finite non-negative weights with at least one positive fusion weight, `reciprocal_rank_constant >= 1`, `0 <= reranker_threshold <= 1`, `0 < bm25_k1 <= 10`, and `0 <= bm25_b <= 1`.

- [ ] **Step 3: Implement Ed25519 signing without fallback algorithms**

Add a direct dependency:

```toml
"cryptography>=45,<50",
```

Use raw 32-byte keys encoded as strict base64. Do not serialize PEM, accept an algorithm field, or silently substitute another primitive.

```python
@dataclass(frozen=True)
class RecallTrustRoot:
    key_id: str
    public_key_ed25519_base64: str

@dataclass(frozen=True)
class RecallSigningKey:
    key_id: str
    private_key_ed25519_base64: str

@dataclass(frozen=True)
class VerifiedManifest:
    manifest: OrganizationManifest
    manifest_digest: str
    canonical_bytes: bytes

def sign_manifest(
    manifest: OrganizationManifest,
    signing_key: RecallSigningKey,
) -> SignedOrganizationManifest:
    if manifest.key_id != signing_key.key_id:
        raise RecallSignatureError("manifest_key_mismatch")
    payload = canonical_json_bytes(manifest.to_dict())
    signature = Ed25519PrivateKey.from_private_bytes(
        signing_key.private_key_bytes()
    ).sign(payload)
    return SignedOrganizationManifest(manifest, _strict_b64encode(signature))

def verify_manifest(
    envelope: SignedOrganizationManifest,
    trust_root: RecallTrustRoot,
) -> VerifiedManifest:
    if envelope.manifest.key_id != trust_root.key_id:
        raise RecallSignatureError("unknown_recall_key")
    payload = canonical_json_bytes(envelope.manifest.to_dict())
    Ed25519PublicKey.from_public_bytes(trust_root.public_key_bytes()).verify(
        envelope.signature_bytes(), payload
    )
    return VerifiedManifest(
        envelope.manifest,
        hashlib.sha256(payload).hexdigest(),
        payload,
    )
```

Map `InvalidSignature` to sanitized `RecallSignatureError("manifest_signature_invalid")` and never include key material or payload bytes in an exception.

- [ ] **Step 4: Run focused and full contract tests**

```bash
.venv/bin/python -m unittest tests.test_recall_contracts -v
.venv/bin/python -m unittest tests.test_jsonio -v
```

Expected: PASS, including a proof that the exact bytes signed are produced by the repository canonical JSON contract.

- [ ] **Step 5: Commit the contract boundary**

```bash
git add pyproject.toml src/zdecision/recall/__init__.py src/zdecision/recall/contracts.py src/zdecision/recall/signing.py tests/test_recall_contracts.py
git commit -m "feat(recall): add signed distribution contracts"
```

---

### Task 2: Build complete Central generations from the verified projection

**Files:**

- Modify: `src/zdecision/central/web/schema.py`
- Modify: `src/zdecision/central/store.py`
- Create: `src/zdecision/central/recall_distribution.py`
- Create: `tests/test_recall_distribution.py`
- Modify: `tests/test_registry_projection.py`

- [ ] **Step 1: Write failing publisher and persistence tests**

The tests must prove:

1. `load_active()` returning `None` prevents signing and pointer advancement.
2. An enabled leaf maps only through `LeafDecisionSpace.compatibility_product_id` to the matching verified product and active heads.
3. Every enabled leaf appears, including an empty leaf; disabled leaves do not.
4. First publication uses organization generation `1` and leaf `decision_version=1`.
5. Replaying the same tree/catalog/profile is idempotent; a same-tree code-only commit does not advance.
6. Changed leaf content advances its leaf version; unchanged leaves reuse version/digest; the organization advances once.
7. Catalog or profile changes advance the organization generation without fabricating a leaf change.
8. Renewal creates a higher generation with unchanged content digests and a new signed lease.
9. A V1 active tuple removed from the next projection is simply absent from the next complete active-head set; Central writes no synthetic history or invalidation list.
10. The publisher never emits revision/retirement/supersession/invalidation records and never accepts non-`active` projection data.
11. A database fault before pointer update leaves the prior current manifest and all referenced objects readable.

Run the RED test:

```bash
.venv/bin/python -m unittest tests.test_recall_distribution -v
```

Expected: FAIL because the distribution store and publisher do not exist.

- [ ] **Step 2: Add immutable recall tables to the existing schema initializer**

Append the following table ownership to `WEB_SCHEMA`; `CentralStore.open()` already invokes `initialize_web_schema()`, so do not create a second migration runner.

```sql
CREATE TABLE IF NOT EXISTS recall_distribution_state (
  organization_id TEXT PRIMARY KEY,
  current_generation INTEGER NOT NULL CHECK(current_generation >= 0),
  current_manifest_digest TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall_catalog_blobs (
  organization_id TEXT NOT NULL,
  catalog_version TEXT NOT NULL,
  catalog_json TEXT NOT NULL,
  PRIMARY KEY(organization_id, catalog_version)
);

CREATE TABLE IF NOT EXISTS recall_profile_blobs (
  organization_id TEXT NOT NULL,
  profile_digest TEXT NOT NULL,
  retrieval_profile_id TEXT NOT NULL,
  profile_json TEXT NOT NULL,
  PRIMARY KEY(organization_id, profile_digest)
);

CREATE TABLE IF NOT EXISTS recall_leaf_snapshots (
  organization_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  decision_version INTEGER NOT NULL CHECK(decision_version > 0),
  snapshot_digest TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  PRIMARY KEY(organization_id, decision_space_id, decision_version),
  UNIQUE(organization_id, decision_space_id, snapshot_digest)
);

CREATE TABLE IF NOT EXISTS recall_manifests (
  organization_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation > 0),
  manifest_digest TEXT NOT NULL,
  registry_tree_oid TEXT NOT NULL,
  catalog_version TEXT NOT NULL,
  profile_digest TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  envelope_json TEXT NOT NULL,
  PRIMARY KEY(organization_id, generation),
  UNIQUE(organization_id, manifest_digest)
);

CREATE TABLE IF NOT EXISTS recall_manifest_leaves (
  organization_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  decision_space_id TEXT NOT NULL,
  decision_version INTEGER NOT NULL,
  snapshot_digest TEXT NOT NULL,
  PRIMARY KEY(organization_id, generation, decision_space_id),
  FOREIGN KEY(organization_id, generation)
    REFERENCES recall_manifests(organization_id, generation),
  FOREIGN KEY(organization_id, decision_space_id, decision_version)
    REFERENCES recall_leaf_snapshots(
      organization_id, decision_space_id, decision_version
    )
);

CREATE TABLE IF NOT EXISTS recall_artifacts (
  organization_id TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  role TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
  media_type TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  PRIMARY KEY(organization_id, artifact_digest)
);
```

All rows are append-only except `recall_distribution_state`; install catalog, profile, snapshots, manifest, leaf bindings, artifact metadata, and the current pointer in one `immediate()` transaction. Replaying identical primary-key bytes succeeds; a conflicting byte sequence raises `RecallDistributionConflict("recall_blob_conflict")`.

- [ ] **Step 3: Add a deterministic organization catalog reader**

Add this exact method to `CentralStore`:

```python
def load_recall_catalog(self, organization_id: str) -> RecallCatalog:
    """Return all trusted groups and enabled concrete leaves in stable ID order."""
```

It reads `catalog_groups` and `decision_spaces`, reconstructs existing domain dataclasses, filters `LeafDecisionSpace.enabled is True`, rejects duplicate compatibility product ownership, sorts groups by `(sort_order, catalog_group_id)` and leaves by `decision_space_id`, computes `catalog_version`, and does not derive catalog state from repository routes.

- [ ] **Step 4: Implement the profile pack and immutable distribution store**

`RecallProfilePack.load(profile_path, artifact_root)` must require absolute paths, a strict canonical profile JSON document, safe relative artifact paths, exact role/digest/size matches, and resolved artifact paths contained by `artifact_root`. It hashes every artifact before the pack becomes publishable.

Expose only immutable reads to the API layer:

```python
class RecallDistributionStore:
    def current_manifest(self, organization_id: str) -> SignedOrganizationManifest | None: ...
    def catalog(self, organization_id: str, catalog_version: str) -> bytes | None: ...
    def leaf_snapshot(self, organization_id: str, decision_space_id: str, decision_version: int) -> bytes | None: ...
    def profile(self, organization_id: str, profile_digest: str) -> bytes | None: ...
    def artifact(self, organization_id: str, artifact_digest: str) -> ArtifactSource | None: ...
    def install_generation(self, prepared: PreparedCentralGeneration) -> None: ...
```

Every JSON read re-parses the strict contract and requires stored UTF-8 bytes to equal `canonical_json_bytes(value.to_dict())`; corruption returns an explicit error rather than noncanonical bytes.

- [ ] **Step 5: Implement V1 snapshot and generation construction**

Expose this publisher API:

```python
class RecallDistributionPublisher:
    def publish_available(
        self,
        organization_id: str,
        now: datetime,
        *,
        required_commit: str | None = None,
        renew: bool = False,
    ) -> SignedOrganizationManifest | None: ...

    def current_or_renew(
        self,
        organization_id: str,
        now: datetime,
    ) -> SignedOrganizationManifest | None: ...
```

The first line of construction is `projection = self.projections.load_active(organization_id)`. Return the prior unchanged envelope when projection is absent. If `required_commit` is supplied, require `projection.commit_sha == required_commit`; otherwise return without advancement.

For each enabled leaf, select the projected `ProductRegistry` and `DecisionRevision` values owned by its `compatibility_product_id`. Re-canonicalize every `DecisionRevision.to_dict()` and require the digest to match the verified projection. Build active records in `(decision_id, revision, digest)` order.

Construct only the current complete active set, not a history-derived event stream:

```python
next_active_revisions = tuple(
    DistributedDecisionRevision(
        decision_id=revision.decision_id,
        revision=revision.revision,
        digest=hashlib.sha256(
            canonical_json_bytes(revision.to_dict())
        ).hexdigest(),
        lifecycle="active",
        document=revision.to_dict(),
    )
    for revision in sorted(projected_active_heads, key=_revision_identity)
)
```

Do not read prior snapshots while constructing `next_active_revisions`, except to reuse an unchanged leaf version/digest. Do not infer a transition by comparing IDs. After Gate 3 makes a new complete generation recall-ready, Gate 4 compares a pinned exact tuple to that complete set: present means eligible for lease rebinding; absent means immediate local `removed_from_active_heads`.

Build a generation fingerprint from the active Registry tree OID, catalog version, sorted leaf `(decision_space_id, decision_version, snapshot_digest)` bindings, and profile digest. If it matches current state and renewal is not due, return the exact stored envelope. If content changes or renewal is due, use `current_generation + 1`, Central policy values for refresh and expiry, sign once, and install atomically.

- [ ] **Step 6: Run Central publisher tests and projection regressions**

```bash
.venv/bin/python -m unittest tests.test_recall_distribution tests.test_registry_projection -v
```

Expected: PASS. The Registry projection remains V1-only and the recall store never reads Git.

- [ ] **Step 7: Commit Central generation construction**

```bash
git add src/zdecision/central/web/schema.py src/zdecision/central/store.py src/zdecision/central/recall_distribution.py tests/test_recall_distribution.py tests/test_registry_projection.py
git commit -m "feat(recall): publish complete trusted generations"
```

---

### Task 3: Expose authenticated canonical distribution and wire exact publication trees

**Files:**

- Modify: `src/zdecision/central/api.py`
- Modify: `src/zdecision/central/cli.py`
- Modify: `src/zdecision/central/web/application.py`
- Modify: `src/zdecision/agent/service.py`
- Modify: `tests/test_central_web_api.py`
- Modify: `tests/test_central_cli.py`
- Modify: `tests/test_demo_config.py`
- Modify: `tests/test_agent_service.py`
- Modify: `tests/integration/test_central_web_vertical.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing API, configuration, startup, and Publication-hook tests**

Add tests for the five device-authenticated routes, exact canonical response bytes, cross-organization isolation, missing-object 404, unavailable-without-LKG 503, stale-but-unchanged LKG serving, bounded artifact streaming, and absence of Git calls during GET requests.

Add hook tests proving:

- startup publishes only after `_synchronize_registry_on_startup()` returns an available projection;
- `_synchronize_completed_publication()` publishes only when the projection's active commit equals `publication.commit_sha`;
- projection failure does not advance recall;
- Publication replay is idempotent; and
- same-tree provenance updates do not create content generations.

Run the RED tests:

```bash
.venv/bin/python -m unittest tests.test_central_web_api tests.test_central_cli tests.test_demo_config tests.test_agent_service -v
```

Expected: FAIL because no recall routes or key configuration exist.

- [ ] **Step 2: Install an explicit private/public Ed25519 configuration split**

Extend `demo-config init` with required `--recall-profile` and `--recall-artifact-root` absolute paths. Generate one Ed25519 keypair and write these strict shapes:

```jsonc
// central.json only
"recall": {
  "key_id": "recall_demo_v1",
  "private_key_ed25519_base64": "<raw-32-byte-base64>",
  "profile_path": "/absolute/profile.json",
  "artifact_root": "/absolute/artifacts",
  "refresh_interval_seconds": 300,
  "lease_seconds": 86400,
  "renew_before_seconds": 3600
}
```

```jsonc
// agent.json only
"recall_trust_root": {
  "key_id": "recall_demo_v1",
  "public_key_ed25519_base64": "<raw-32-byte-base64>"
}
```

Both files remain mode `0600`. `_load_central_config()` validates policy ranges, absolute pack paths, and the private raw key. In this task, add `recall_trust_root: RecallTrustRoot` to `AgentConfig` and the exact field set in `load_agent_config()`; the private key field is invalid Agent input. Tests must explicitly assert that the private key never appears in `agent.json` and that the Agent token never appears in `central.json`. Update the README demo command with the two explicit pack arguments; do not bundle or silently choose a production model.

- [ ] **Step 3: Add the exact authenticated API surface**

Pass an optional `RecallDistributionService` into `create_app()` and add:

```text
GET /api/v1/agent/recall/manifest
GET /api/v1/agent/recall/catalogs/{catalog_version}
GET /api/v1/agent/recall/leaves/{decision_space_id}/versions/{decision_version}
GET /api/v1/agent/recall/profiles/{profile_digest}
GET /api/v1/agent/recall/artifacts/{artifact_digest}
```

Each handler calls `device(authorization)` first and derives organization identity only from the authenticated `Principal`. Return JSON using `Response(content=canonical_bytes, media_type="application/json")`, not `JSONResponse`. The artifact route returns a `StreamingResponse`, `Content-Length`, `ETag: "sha256:<digest>"`, and `Cache-Control: private, immutable`; it may open only the already-verified resolved `ArtifactSource` recorded for that organization.

The manifest route calls `publisher.current_or_renew(principal.organization_id, current_time())`. That method may consult SQLite projection state but never Git. When the projection is unavailable, it returns the stored envelope byte-for-byte without renewal; if none exists, return sanitized `503 {"error":"recall_not_ready"}`.

- [ ] **Step 4: Publish only after exact-tree projection synchronization**

Make the existing synchronizer return value the gate in both wiring points:

```python
state = self.registry_synchronizer.synchronize(
    publication.organization_id,
    publication.commit_sha,
    publication.updated_at,
)
if state.state == "available" and state.active_commit == publication.commit_sha:
    self.recall_publisher.publish_available(
        publication.organization_id,
        _parse_utc(publication.updated_at),
        required_commit=publication.commit_sha,
    )
```

Apply the same condition after startup synchronization. Construct `RecallProfilePack`, `RecallDistributionStore`, publisher, and service once in `_run_server()`, sharing the existing SQLite connection. Do not publish before catalog rows and the verified projection are installed.

- [ ] **Step 5: Run API and publication vertical tests**

```bash
.venv/bin/python -m unittest tests.test_central_web_api tests.test_central_cli tests.test_demo_config tests.test_agent_service tests.integration.test_central_web_vertical -v
```

Expected: PASS, including exact-commit advancement and authenticated canonical reads.

- [ ] **Step 6: Commit the Central delivery boundary**

```bash
git add src/zdecision/central/api.py src/zdecision/central/cli.py src/zdecision/central/web/application.py src/zdecision/agent/service.py tests/test_central_web_api.py tests/test_central_cli.py tests/test_demo_config.py tests/test_agent_service.py tests/integration/test_central_web_vertical.py README.md
git commit -m "feat(recall): serve authenticated signed snapshots"
```

---

### Task 4: Add durable Agent high-water, clock safety, LKG, and cache reads

**Files:**

- Create: `src/zdecision/agent/recall_clock.py`
- Create: `src/zdecision/agent/recall_cache.py`
- Create: `tests/test_recall_clock.py`
- Create: `tests/test_recall_cache.py`

- [ ] **Step 1: Write failing clock and cache transition tests**

Cover fresh valid lease, wall time past expiry, wall rollback while running, restart behind persisted trusted time, monotonic time preventing a backward wall jump from extending validity, valid higher signed generation re-establishing trust, lower-generation rejection, same-generation/different-digest rejection, same-generation/same-digest resume, high-water advancement before downloads, atomic trusted pointer behavior, corrupt trusted data, failed refresh with valid LKG, expired LKG, and a complete newer V1 set that omits a formerly active exact tuple.

Include this required current-producer assertion. Gate 2 proves complete-set absence without manufacturing metadata; Gate 4 maps that absence to `removed_from_active_heads` only after Gate 3 makes the corresponding generation recall-ready.

```python
old_bundle = cache.trusted_bundle_for_generation(1, now=VALID_GEN_2_TIME)
new_bundle = cache.trusted_bundle(now=VALID_GEN_2_TIME)
self.assertTrue(old_bundle.contains_active_head(SPACE, DECISION, 1, OLD_DIGEST))
self.assertFalse(new_bundle.contains_active_head(SPACE, DECISION, 1, OLD_DIGEST))
```

Run the RED tests:

```bash
.venv/bin/python -m unittest tests.test_recall_clock tests.test_recall_cache -v
```

Expected: FAIL because the clock and cache modules do not exist.

- [ ] **Step 2: Implement signed-time and monotonic lease assessment**

Use injected aware wall and monotonic clocks. Persist `greatest_signed_issued_at` and `greatest_trusted_wall_at`; never persist a monotonic value across process restarts.

```python
@dataclass(frozen=True)
class LeaseAssessment:
    state: Literal["valid", "expired", "clock_untrusted"]
    effective_now: str
    monotonic_deadline: float | None

class RecallLeaseClock:
    def assess_existing(
        self,
        *,
        issued_at: str,
        expires_at: str,
        greatest_trusted_wall_at: str,
    ) -> LeaseAssessment: ...

    def establish_from_newer_signed_manifest(
        self,
        *,
        issued_at: str,
        expires_at: str,
    ) -> LeaseAssessment: ...
```

For an already accepted generation, effective remaining validity is the minimum of signed wall-clock remaining time and the in-process monotonic deadline. If the current wall clock is earlier than the persisted trusted floor after restart, return `clock_untrusted`. A signature-valid strictly higher generation with `issued_at >= greatest_signed_issued_at` may establish a new monotonic deadline from its signed interval and move the durable floor forward. It may not revive the same or a lower generation.

- [ ] **Step 3: Create focused Agent recall tables**

`RecallCacheStore.open(database_path, cache_root)` opens the existing Agent SQLite file with foreign keys and WAL, like the other focused stores. Add these owned tables in its initializer:

```sql
recall_organization_state(
  organization_id PRIMARY KEY,
  highest_generation, highest_manifest_digest,
  greatest_signed_issued_at, greatest_trusted_wall_at,
  trusted_generation, refresh_state, last_error_code, next_refresh_at
)
recall_agent_manifests(
  organization_id, generation, manifest_digest, envelope_json,
  issued_at, expires_at, acceptance_state,
  PRIMARY KEY(organization_id, generation)
)
recall_agent_catalogs(
  organization_id, catalog_version, catalog_json,
  PRIMARY KEY(organization_id, catalog_version)
)
recall_agent_leaf_snapshots(
  organization_id, decision_space_id, decision_version,
  snapshot_digest, snapshot_json,
  PRIMARY KEY(organization_id, decision_space_id, decision_version)
)
recall_agent_profiles(
  organization_id, profile_digest, profile_json,
  PRIMARY KEY(organization_id, profile_digest)
)
recall_agent_artifacts(
  organization_id, artifact_digest, size_bytes, absolute_path, validated_at,
  PRIMARY KEY(organization_id, artifact_digest)
)
recall_agent_generation_leaves(
  organization_id, generation, decision_space_id, decision_version,
  snapshot_digest,
  PRIMARY KEY(organization_id, generation, decision_space_id)
)
recall_agent_decision_blobs(
  organization_id, decision_space_id, decision_id, revision, digest,
  canonical_json,
  PRIMARY KEY(organization_id, decision_space_id, decision_id, revision, digest)
)
recall_agent_generation_decisions(
  organization_id, generation, decision_space_id, decision_id, revision, digest,
  PRIMARY KEY(organization_id, generation, decision_space_id, decision_id, revision, digest)
)
```

Use real column types, `CHECK` constraints, and foreign keys in the implementation. The compact declaration above fixes ownership and key shape. Retain immutable Decision blobs, artifact files, and generation rows; Gate 2 performs no garbage collection, so a later Gate 3 recall-ready pointer or Session pin cannot lose referenced bytes.

- [ ] **Step 4: Implement high-water staging and one-pointer activation**

Expose these methods exactly:

```python
class RecallCacheStore:
    def accept_manifest(
        self, verified: VerifiedManifest, envelope: SignedOrganizationManifest,
        assessment: LeaseAssessment, accepted_at: str,
    ) -> Literal["new", "resume", "active"]: ...
    def stage_catalog(self, generation: int, catalog: RecallCatalog) -> None: ...
    def stage_leaf(self, generation: int, snapshot: LeafDecisionSnapshot) -> None: ...
    def stage_profile(self, generation: int, profile: RetrievalProfileManifest) -> None: ...
    def record_artifact(self, generation: int, binding: ArtifactBinding) -> None: ...
    def activate_trusted(
        self, generation: int, activated_at: str,
    ) -> TrustedRecallBundle: ...
    def readiness(self, now: datetime) -> RecallReadiness: ...
    def trusted_bundle(self, now: datetime) -> TrustedRecallBundle | None: ...
    def trusted_bundle_for_generation(
        self, generation: int, now: datetime,
    ) -> TrustedRecallBundle | None: ...
    def record_refresh_failure(self, code: str, next_refresh_at: str) -> None: ...
```

`accept_manifest()` verifies lower/same-generation rules in one immediate transaction and advances high-water without touching `trusted_generation`. `activate_trusted()` re-reads every staged canonical object, count, digest, catalog ownership, artifact role/digest/size, and Decision coverage inside one transaction, then changes only `trusted_generation` and state to `trusted_data_ready`. Readers query through that pointer and therefore observe the complete old or complete new signed data generation. Gate 3 must use a different recall-ready pointer after its runtime, tokenizer, dimension, schema, coverage, index, and query-smoke checks.

Use these handoff shapes:

```python
@dataclass(frozen=True)
class TrustedDecisionRevision:
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str
    source_generation: int
    canonical_json: bytes

@dataclass(frozen=True)
class ArtifactBinding:
    role: str
    digest: str
    size_bytes: int
    absolute_path: Path

@dataclass(frozen=True)
class TrustedLeafSnapshot:
    decision_space_id: str
    decision_version: int
    snapshot_digest: str
    snapshot: LeafDecisionSnapshot

@dataclass(frozen=True)
class RecallReadiness:
    state: Literal[
        "trusted_data_ready", "trusted_data_degraded", "preparing", "cold",
        "expired", "invalid", "rollback_detected", "clock_untrusted",
    ]
    generation: int | None
    expires_at: str | None
    last_error_code: str | None
    next_refresh_at: str | None

@dataclass(frozen=True)
class TrustedRecallBundle:
    organization_id: str
    generation: int
    manifest_digest: str
    registry_tree_oid: str
    catalog_version: str
    retrieval_profile_digest: str
    retrieval_profile: RetrievalProfileManifest
    expires_at: str
    freshness: Literal["current", "degraded"]
    artifacts: tuple[ArtifactBinding, ...]
    leaves: tuple[TrustedLeafSnapshot, ...]
    decisions: tuple[TrustedDecisionRevision, ...]

    def contains_active_head(
        self,
        decision_space_id: str,
        decision_id: str,
        revision: int,
        digest: str,
    ) -> bool: ...
```

`TrustedRecallBundle.decisions` is a stable flattened view of every leaf's complete active-head set with canonical bytes recomputed from the signed document. `contains_active_head()` is an exact membership test; it does not classify why a tuple is absent. Gate 2 does not resolve Session pins. Gate 4 owns the current V1 mapping from absence to `removed_from_active_heads` after Gate 3 recall activation, and any future ordinary-revision transition remains outside the current producer claim.

- [ ] **Step 5: Run clock/cache tests**

```bash
.venv/bin/python -m unittest tests.test_recall_clock tests.test_recall_cache -v
```

Expected: PASS with old trusted-data LKG remaining available after failed staging and stopping exactly at its signed/monotonic boundary. The successful end state is `trusted_data_ready`, never `ready`.

- [ ] **Step 6: Commit durable Agent state**

```bash
git add src/zdecision/agent/recall_clock.py src/zdecision/agent/recall_cache.py tests/test_recall_clock.py tests/test_recall_cache.py
git commit -m "feat(recall): add atomic trusted agent cache"
```

---

### Task 5: Stream and stage generic content-addressed artifacts

**Files:**

- Create: `src/zdecision/agent/recall_artifacts.py`
- Create: `tests/recall_fixtures.py`
- Create: `tests/test_recall_artifacts.py`

- [ ] **Step 1: Write failing artifact staging tests**

Test chunked download without buffering, declared-size overflow, short read, digest mismatch, corrupt existing content-addressed file, temp cleanup, atomic replacement, safe target containment, four required artifact roles, exact profile role/digest/size matching, verified-file reuse, and a failed new pack retaining the old trusted-data LKG.

Run the RED tests:

```bash
.venv/bin/python -m unittest tests.test_recall_artifacts -v
```

Expected: FAIL because the downloader does not exist.

- [ ] **Step 2: Implement content-addressed streaming installation**

Expose:

```python
class RecallArtifactDownloader:
    def __init__(self, root: Path) -> None: ...
    def install(
        self,
        artifact: RetrievalArtifact,
        chunks: Iterable[bytes],
    ) -> ArtifactBinding: ...
```

Resolve targets as `<root>/sha256/<first-two>/<digest>`, create an owner-only temporary file in the same directory with `O_EXCL`, reject zero-length chunks, update SHA-256 and byte count per chunk, stop immediately above declared size, require exact size and digest, flush and `fsync()` the file, `os.replace()` it, then `fsync()` the parent directory. A verified existing target is reused. Any failure removes only that invocation's temporary file and returns a sanitized code; never log URLs, tokens, or bytes.

- [ ] **Step 3: Provide a deterministic non-model fixture pack**

`tests/recall_fixtures.py` supplies `make_fixture_profile_pack(root)`. It writes four small deterministic opaque byte files, computes their real digests/sizes, and returns a fully populated `RetrievalProfileManifest`. The bytes deliberately are not loadable production models or tokenizers; their purpose is to exercise the exact content-addressed transport and cache path without selecting a model or runtime.

```python
def make_fixture_profile_pack(root: Path) -> tuple[
    RetrievalProfileManifest,
    Mapping[str, Path],
]: ...
```

Assert in the fixture test that importing or calling the Gate 2 modules neither imports `onnxruntime`/`tokenizers` nor constructs an index. Gate 3 will define the production `RetrievalBackend`, select the one runtime dependency, load tokenizers/models, validate dimension/schema/coverage, build every leaf index, run its query smoke check, and atomically publish a recall-ready generation.

- [ ] **Step 4: Run artifact staging tests**

```bash
.venv/bin/python -m unittest tests.test_recall_artifacts -v
```

Expected: PASS. A corrupt or incomplete new pack leaves prior immutable artifact paths and the trusted-data pointer unchanged.

- [ ] **Step 5: Commit generic artifact staging**

```bash
git add src/zdecision/agent/recall_artifacts.py tests/recall_fixtures.py tests/test_recall_artifacts.py
git commit -m "feat(recall): stage content-addressed artifacts"
```

---

### Task 6: Synchronize in the persistent Agent service without Prompt traffic

**Files:**

- Modify: `src/zdecision/agent/central_client.py`
- Modify: `src/zdecision/agent/service.py`
- Modify: `src/zdecision/agent/cli.py`
- Create: `src/zdecision/agent/recall_sync.py`
- Create: `tests/test_recall_sync.py`
- Modify: `tests/test_central_client.py`
- Modify: `tests/test_agent_service.py`
- Modify: `tests/test_launchd.py`

- [ ] **Step 1: Write failing transport, sync-order, privacy, and scheduling tests**

Tests must prove:

- all five recall reads carry the existing bearer device token and expose only organization-scoped content identifiers;
- manifest/profile responses are bounded to 1 MiB, catalog to 4 MiB, and each leaf snapshot to 32 MiB;
- artifacts stream and stop at their signed declared size without using `_request()`'s response buffer;
- signature and organization validation precede `accept_manifest()`;
- high-water acceptance precedes catalog/snapshot/profile/artifact GETs;
- each downloaded object is canonical and matches the signed reference before staging;
- the profile, all enabled leaves, and all referenced artifact bytes verify before trusted-data activation;
- transient failure records a sanitized code and schedules retry while a valid LKG remains readable;
- first service iteration synchronizes immediately, later calls wait for signed refresh policy, and a newly enabled leaf causes `preparing` instead of a false `trusted_data_ready`; and
- the synchronizer API and recorded requests contain no Session ID, Turn ID, Prompt, PRD, local path, query, embedding, candidate, score, or active injected set.

Run the RED tests:

```bash
.venv/bin/python -m unittest tests.test_recall_sync tests.test_central_client tests.test_agent_service -v
```

Expected: FAIL because the recall client and synchronizer are absent.

- [ ] **Step 2: Add bounded canonical and streaming Central client methods**

Expose:

```python
def get_recall_manifest(self) -> SignedOrganizationManifest: ...
def get_recall_catalog(self, catalog_version: str) -> RecallCatalog: ...
def get_recall_leaf(
    self, decision_space_id: str, decision_version: int,
) -> LeafDecisionSnapshot: ...
def get_recall_profile(self, profile_digest: str) -> RetrievalProfileManifest: ...

@contextmanager
def stream_recall_artifact(
    self, artifact_digest: str,
) -> Iterator[Iterable[bytes]]: ...
```

For JSON endpoints, enforce the endpoint-specific limit while reading, parse strict JSON, reconstruct the contract, and require received bytes to equal `canonical_json_bytes(value.to_dict())`. For artifacts, use `httpx.Client.stream()`, require status 200, validate `Content-Length` and ETag against the signed artifact before yielding chunks, and let the downloader enforce actual length/digest. Retry a stream only by opening a fresh response and a fresh temporary file; never append to partial bytes.

- [ ] **Step 3: Implement the synchronization state machine in one direction**

```python
@dataclass(frozen=True)
class RecallSyncResult:
    state: Literal[
        "trusted_data_activated", "unchanged", "retry_scheduled", "rejected"
    ]
    generation: int | None
    error_code: str | None
    next_refresh_at: str

class RecallSynchronizer:
    def sync_once(self, now: datetime) -> RecallSyncResult: ...
    def sync_if_due(self, now: datetime) -> RecallSyncResult | None: ...
```

`sync_once()` performs this exact order:

1. Fetch and strictly parse the signed envelope.
2. Verify Ed25519 with the configured `RecallTrustRoot` and require manifest organization equality.
3. Obtain a lease assessment; reject expired/untrusted input except that a strictly newer signed generation may re-establish clock trust.
4. Call `cache.accept_manifest()` so rollback/freeze high-water is durable.
5. If the same generation is already the complete trusted generation, update schedule only and return `unchanged`.
6. Fetch and validate the catalog version.
7. Fetch every manifest leaf, validating space/product ownership, decision version, snapshot digest, active count/manifest, and the complete V1 active-head content. Do not fetch or synthesize transition history.
8. Fetch and validate the profile digest and all signed control bounds.
9. Reuse verified content-addressed artifacts or stream-install missing ones.
10. Stage canonical catalog, leaves, profile, Decisions, and artifact bindings.
11. Call `cache.activate_trusted()` once, yielding only `trusted_data_ready`, then schedule the next request from the signed `refresh_interval_seconds`.

On any failure after step 4, keep the high-water pending record, call `record_refresh_failure()` with a closed error-code vocabulary, and retain only an independently valid LKG. Never downgrade to keyword-only or activate a subset.

- [ ] **Step 4: Use only the configured public trust root**

Pass the already validated `AgentConfig.recall_trust_root` from Task 3 into `RecallSynchronizer`. Continue requiring an absolute owner-owned mode-0600 config file. Do not read a key from Central responses, the artifact pack, environment variables, or cached metadata.

- [ ] **Step 5: Wire startup prefetch and signed scheduling into the existing service**

Extend, rather than replace, the persistent delivery loop:

```python
class AgentService:
    def __init__(
        self,
        *,
        client: CentralClient,
        processor: CaptureRequestProcessor | None,
        recall_synchronizer: RecallSynchronizer | None,
        lease_client_factory: Callable[[], object],
        ...,
    ) -> None: ...

    def run_once(self) -> bool:
        recall_work = self._sync_recall_if_due()
        capture_work = self._process_capture_once()
        return recall_work or capture_work
```

Construct `RecallCacheStore` below the existing `database_path`, content cache below `state_path.parent / "recall"`, `RecallArtifactDownloader`, and the synchronizer in `_run_service_command()`. Do not construct a model runtime, backend factory, profile manager, or index builder in Gate 2. The first run is due immediately. The five-second idle loop may check local due state, but only `sync_if_due()` performs a Central request, using the signed five-minute policy after trusted-data activation. LaunchAgent installation remains the onboarding start boundary.

Do not wire recall network access into `hook`, `mcp`, `Worker(sync_poller=None)`, Prompt submission, or an inline refresh tool. Gate 3 later reads `RecallReadiness` and `TrustedRecallBundle` locally and publishes a separate recall-ready bundle after full model/index validation.

- [ ] **Step 6: Run Agent transport/service regressions**

```bash
.venv/bin/python -m unittest tests.test_recall_sync tests.test_central_client tests.test_agent_service tests.test_launchd -v
```

Expected: PASS, with first-run prefetch, a `trusted_data_ready` end state, and no Prompt-derived request field.

- [ ] **Step 7: Commit persistent synchronization**

```bash
git add src/zdecision/agent/central_client.py src/zdecision/agent/service.py src/zdecision/agent/cli.py src/zdecision/agent/recall_sync.py tests/test_recall_sync.py tests/test_central_client.py tests/test_agent_service.py tests/test_launchd.py
git commit -m "feat(recall): sync signed generations in agent service"
```

---

### Task 7: Prove the clean-device Gate 2 lifecycle end to end

**Files:**

- Create: `tests/integration/test_trusted_recall_onboarding.py`
- Modify: `tests/test_recall_distribution.py`
- Modify: `tests/test_recall_cache.py`
- Modify: `tests/test_recall_sync.py`
- Modify: `README.md`

- [ ] **Step 1: Write the end-to-end acceptance fixture before changing implementation**

The test creates temporary Central and Agent databases, an empty Agent cache root, a canonical deterministic four-file opaque artifact pack, an Ed25519 keypair, an available verified projection, and a trusted enabled-leaf catalog. Start the real FastAPI application on an ephemeral loopback port and use the production `CentralClient`. There is no retrieval backend seam in Gate 2.

The first scenario must prove:

```text
empty Agent cache
  -> authenticated manifest GET
  -> Ed25519 verification and generation-1 high-water
  -> canonical catalog + every enabled leaf + profile fetch
  -> streamed SHA-256 artifact installation
  -> one atomic trusted-generation switch
  -> RecallReadiness.state == "trusted_data_ready"
  -> TrustedRecallBundle containing every full canonical active r1 Decision
```

Run the acceptance test and retain the initial failure message:

```bash
.venv/bin/python -m unittest tests.integration.test_trusted_recall_onboarding -v
```

Expected at this point: FAIL at the first missing or incorrectly wired lifecycle invariant, not from a seeded cache.

- [ ] **Step 2: Add the complete failure and transition matrix**

Use subtests with fresh temporary roots for:

- wrong Ed25519 key, unknown key ID, signature mutation, and canonical manifest mutation;
- lower generation and same-generation/different-digest rollback/freeze attempts;
- partial leaf list, wrong catalog ownership, wrong Decision digest/count, corrupt artifact, and missing artifact role;
- failed generation 2 preserving a still-valid generation 1 LKG;
- generation 1 expiry stopping reads after generation 2 failure;
- backward wall-clock jump and restart returning `clock_untrusted`;
- a valid higher signed generation re-establishing time trust;
- publication not advancing before its exact projection tree is active;
- code-only same-tree synchronization remaining at the same content generation until renewal;
- renewal minting a higher generation with reused leaf/artifact blobs;
- an exact r1 tuple present in both trusted complete sets remaining available for Gate 3 lease rebinding; and
- deletion of an r1 active head producing a newer complete set in which the exact old tuple is absent, without a Central-synthesized invalidation record.

Do not manufacture an r2 Registry document or transition payload in this suite. Future ordinary-revision representation belongs to the later local transition type, not to the current signed V1 distribution schema.

- [ ] **Step 3: Close implementation gaps exposed by the acceptance test**

Make only lifecycle corrections in the files already introduced by Tasks 1–6. Keep the test's cache root empty before the first sync and assert that all deterministic artifact bytes were fetched during the test process. Assert that no index directory is created and no model runtime is imported.

Document the Gate boundary in README:

```text
Gate 2 proves trusted signed distribution, generic content-addressed artifact
staging, and atomic `trusted_data_ready` cache activation with deterministic
opaque fixture bytes. It does not select or load a production model, build an
index, or claim recall readiness; Gate 3 owns those steps. Registry V1 produces
r1 active heads only. After Gate 3 activates a newer complete set, absence of an
exact previously active tuple locally means `removed_from_active_heads`.
```

- [ ] **Step 4: Run the focused Gate 2 matrix**

```bash
.venv/bin/python -m unittest tests.test_recall_contracts tests.test_recall_distribution tests.test_recall_clock tests.test_recall_cache tests.test_recall_artifacts tests.test_recall_sync tests.integration.test_trusted_recall_onboarding -v
```

Expected: PASS with no seeded Agent cache and no Registry V2 fixture.

- [ ] **Step 5: Run all repository tests and inspect the diff**

```bash
.venv/bin/python -m unittest discover -s tests -v
git diff --check
git status --short
```

Expected: all tests pass; `git diff --check` prints nothing; status contains only the exact Gate 2 files plus any pre-existing user-owned changes that were never staged by the plan's exact `git add` commands.

- [ ] **Step 6: Commit Gate 2 acceptance**

```bash
git add tests/integration/test_trusted_recall_onboarding.py tests/test_recall_distribution.py tests/test_recall_cache.py tests/test_recall_sync.py README.md
git commit -m "test(recall): prove trusted clean-device onboarding"
```

---

## Gate 2 Completion Evidence

Before reporting Gate 2 complete, attach the exact output of the focused matrix and full discovery commands, plus a short mapping to these claims:

| Claim | Required evidence |
|---|---|
| Authenticity | correct-key pass; wrong/unknown/tampered Ed25519 cases fail closed |
| Canonical completeness | catalog, every enabled leaf, Decision digests/counts, profile, and every referenced generic artifact validated |
| Anti-rollback/freeze | lower generation and same-generation/different-digest rejected after durable high-water |
| Atomicity and LKG | induced failures before pointer switch retain only the complete prior generation |
| Freshness | expiry, monotonic deadline, backward wall clock, restart, and higher-signed-generation recovery pass |
| Publication ordering | exact Publication commit/tree is active before generation advancement |
| V1 removal fact | old r1 tuple is absent from the new complete set; no Central history or invalidation record is synthesized |
| V1 scope honesty | no Registry V2 change and no ordinary-revision producer claim |
| Privacy | only authenticated organization/content identifiers appear in Central request observations |
| Gate 3 handoff | `TrustedRecallBundle` exposes immutable profile/artifact bindings and the complete canonical per-leaf active-head set in `trusted_data_ready` state |

Do not mark Gate 2 complete if `trusted_data_ready` is reported as recall-ready, if opaque fixture bytes are mistaken for a production model, if a production runtime/index is added here, if a manifest was seeded into the Agent cache, if a partial generation was made visible, if Central synthesizes invalidation history, or if an ordinary r2 transition is claimed from the current V1 Registry.
