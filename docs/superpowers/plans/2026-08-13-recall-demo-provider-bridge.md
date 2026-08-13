# Recall Demo Provider Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the validated local Recall Demo to the existing Candidate → Decision Center → publish → Gate A flow so one newly published `third-party-services` Decision can be recalled and applied in a new `zstack-ui-next` task.

**Architecture:** Promote the frozen prototype into an installable `zdecision.recall.demo` package, publish immutable signed bundle generations after completed Central publication, and implement the existing `RecallProvider` seam with a Hook-safe preflight plus an MCP-only local retrieval path. Both Hook and MCP load the same owner-only configuration; missing or invalid Demo state continues to use `UnavailableRecallProvider` and fails closed.

**Tech Stack:** Python 3.11+, `unittest`, Ed25519 via `cryptography`, pinned Hugging Face model snapshots, `torch`, `transformers`, existing SQLite/Central/Gate A services, Codex Hooks and MCP Apps.

**Spec:** `docs/superpowers/specs/2026-08-13-recall-demo-provider-bridge-design.md`

## Global Constraints

- This is a leadership Demo, not Gate B, Gate C, Gate D, or a production-readiness claim.
- Support only repository `zstack-ui-next`, product `third-party-services`, and Decision Space `prod_3e6e73b8defbfee89ce7bf26e739b1dc`.
- Accept 1 through 32 unique active formal Decision heads and every positive head revision; ignore non-active heads.
- Preserve the existing shortlist bounds: at most 8 complete Decisions and at most 10,000 UTF-8 bytes.
- Recall is offline. Never download a model, call Central, call a network endpoint, read Git, or mutate the Registry from `preflight()` or `retrieve()`.
- `preflight()` must not import Torch, load models, build an index, or expose Decision prose, private paths, keys, session IDs, or turn IDs.
- `retrieve()` runs only after trusted card consent and must revalidate the exact bundle, profile, model installation, and generation frozen by preflight.
- A completed publication refreshes the Demo bundle automatically; any refresh failure preserves the previous `current.json` and returns bounded code `recall_demo_refresh_failed`.
- The private signing key remains outside Git and outside every bundle. Hook/MCP provider code never reads it.
- When Demo configuration is absent or invalid, keep current behavior by constructing `UnavailableRecallProvider`.
- Do not alter Candidate card semantics, Central review semantics, Gate A confirmation/application semantics, or mutation gating.
- Do not touch or stage the pre-existing untracked files `docs/superpowers/acceptance/2026-08-06-recall-host-gate.md`, `tests/integration/test_recall_host_gate.py`, or `uv.lock`.
- Do not claim the new Decision is recallable until the signed generation pointer has been committed and verified.

## File Structure

### Installable Demo retrieval package

- `src/zdecision/recall/demo/__init__.py` — public Demo exports only.
- `src/zdecision/recall/demo/contracts.py` — frozen one-product retrieval profile and model bindings.
- `src/zdecision/recall/demo/bundle.py` — signed bundle build and verification.
- `src/zdecision/recall/demo/model_store.py` — prepared local model snapshot verification.
- `src/zdecision/recall/demo/projection.py` — deterministic Decision/intent text projection.
- `src/zdecision/recall/demo/index.py` — bounded in-memory BM25/dense/path index.
- `src/zdecision/recall/demo/retrieval.py` — weighted fusion, reranking, and shortlist packing.
- `src/zdecision/recall/demo/runtime.py` — offline E5/BGE runtime adapters.
- `src/zdecision/recall/demo/cli.py` — operator commands for model preparation and diagnostics.
- `src/zdecision/recall/demo/demo-profile.json` — signed fixed retrieval/model profile.

### Bridge-specific files

- `src/zdecision/recall/demo/config.py` — owner-only configuration and reader/publisher views.
- `src/zdecision/recall/demo/publication.py` — immutable generation metadata, publisher, and atomic pointer.
- `src/zdecision/recall/demo/provider.py` — `DemoRecallProvider` and safe provider factory.
- `docs/demo-recall-provider.md` — exact setup, leadership rehearsal, and rollback commands.

### Existing boundaries modified

- `src/zdecision/agent/cli.py` — configure/status commands and Hook provider injection.
- `src/zdecision/agent/mcp_server.py` — MCP provider injection.
- `src/zdecision/central/cli.py` — optional publisher construction.
- `src/zdecision/central/web/application.py` — completed-publication refresh call.
- `src/zdecision/central/api.py` — bounded refresh failure response.
- `pyproject.toml` — optional Demo dependency group, package data, and CLI entry point.
- `plugins/zdecision/.codex-plugin/plugin.json` and `plugins/zdecision/skills/zdecision/SKILL.md` — honest Demo availability wording.

---

### Task 1: Promote the validated retrieval prototype

**Files:**
- Create: `src/zdecision/recall/demo/__init__.py`
- Create: `src/zdecision/recall/demo/contracts.py`
- Create: `src/zdecision/recall/demo/bundle.py`
- Create: `src/zdecision/recall/demo/model_store.py`
- Create: `src/zdecision/recall/demo/projection.py`
- Create: `src/zdecision/recall/demo/index.py`
- Create: `src/zdecision/recall/demo/retrieval.py`
- Create: `src/zdecision/recall/demo/runtime.py`
- Create: `src/zdecision/recall/demo/cli.py`
- Create: `src/zdecision/recall/demo/demo-profile.json`
- Create: `tests/test_recall_demo_bundle.py`
- Create: `tests/test_recall_demo_models.py`
- Create: `tests/test_recall_demo_projection.py`
- Create: `tests/test_recall_demo_retrieval.py`
- Create: `tests/test_recall_demo_cli.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: validated prototype commit `b6ddbd21fda9fd24e630fa9553ad3c6734bddc24` from worktree `/Users/zhaohuiying/Desktop/Zstack-repos/zdecision-recall-demo-prototype`.
- Produces: importable `zdecision.recall.demo` package with unchanged public classes/functions `DemoRetrievalProfile`, `VerifiedDemoBundle`, `build_signed_bundle`, `load_verified_bundle`, `InstalledModels`, `prepare_models`, `load_installed_models`, `DemoIndex`, `HybridDemoRetriever`, `load_transformers_runtime`, and `main`.

- [ ] **Step 1: Add an import RED before copying implementation**

Add this test first to `tests/test_recall_demo_bundle.py`:

```python
import unittest


class RecallDemoPromotionTests(unittest.TestCase):
    def test_installable_demo_package_exports_bundle_builder(self) -> None:
        from zdecision.recall.demo.bundle import build_signed_bundle

        self.assertTrue(callable(build_signed_bundle))
```

- [ ] **Step 2: Run the import RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_demo_bundle -v
```

Expected: `ModuleNotFoundError: No module named 'zdecision.recall.demo'`.

- [ ] **Step 3: Promote the exact validated files and tests**

Using `apply_patch`, copy the listed files byte-for-byte from prototype commit `b6ddbd21fda9fd24e630fa9553ad3c6734bddc24`, then apply only this import mapping throughout the promoted package and copied tests:

```python
# before
from prototypes.recall_demo.bundle import VerifiedDemoBundle
from prototypes.recall_demo.contracts import DemoRetrievalProfile

# after
from zdecision.recall.demo.bundle import VerifiedDemoBundle
from zdecision.recall.demo.contracts import DemoRetrievalProfile
```

Set the CLI parser program and repository anchor exactly as follows:

```python
parser = argparse.ArgumentParser(prog="zdecision-recall-demo", allow_abbrev=False)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
```

Do not change ranking thresholds, model identities, file hashes, bundle validation, or test expectations in this step.

- [ ] **Step 4: Add install metadata without changing the base installation**

Add the optional dependency table, then add one entry to each existing scripts
and package-data table. The resulting relevant TOML must be:

```toml
[project.optional-dependencies]
recall-demo = [
  "cryptography==49.0.0",
  "huggingface_hub==0.36.2",
  "torch==2.13.0",
  "transformers==4.57.6",
]

[project.scripts]
zdecision = "zdecision.cli:main"
zdecision-agent = "zdecision.agent.cli:main"
zdecision-central = "zdecision.central.cli:main"
zdecision-recall-demo = "zdecision.recall.demo.cli:main"

[tool.setuptools.package-data]
"zdecision.capture" = ["prompt_contracts/*.md"]
"zdecision.central" = ["static/*.html", "static/assets/*"]
"zdecision.agent" = ["static/*.html"]
"zdecision.recall.demo" = ["demo-profile.json"]
```

Do not declare either existing table twice. Do not generate or stage `uv.lock`.

- [ ] **Step 5: Install the Demo extra in the active development environment**

Run:

```bash
.venv/bin/python -m pip install -e '.[recall-demo]'
```

Expected: editable install succeeds and does not modify tracked files. If the
installer creates or changes the pre-existing untracked `uv.lock`, leave it
unmodified and unstaged.

- [ ] **Step 6: Run the promoted prototype suite**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_bundle \
  tests.test_recall_demo_models \
  tests.test_recall_demo_projection \
  tests.test_recall_demo_retrieval \
  tests.test_recall_demo_cli -v
```

Expected: all 123 prototype tests pass; the existing real-model smoke remains skipped unless its explicit environment flag is set.

- [ ] **Step 7: Verify import boundaries**

Run:

```bash
rg -n "prototypes\.recall_demo|sys\.path|PYTHONPATH" src/zdecision/recall/demo tests/test_recall_demo_*.py
.venv/bin/python -m compileall -q src/zdecision/recall/demo
git diff --check
```

Expected: `rg` has no matches; compile and diff checks exit 0.

- [ ] **Step 8: Commit the promotion**

```bash
git add pyproject.toml src/zdecision/recall/demo \
  tests/test_recall_demo_bundle.py tests/test_recall_demo_models.py \
  tests/test_recall_demo_projection.py tests/test_recall_demo_retrieval.py \
  tests/test_recall_demo_cli.py
git commit -m "feat: promote local Recall demo retrieval"
```

---

### Task 2: Add owner-only Demo configuration

**Files:**
- Create: `src/zdecision/recall/demo/config.py`
- Create: `tests/test_recall_demo_config.py`
- Modify: `src/zdecision/agent/cli.py`

**Interfaces:**
- Consumes: `private_state_root(environ)` and the fixed Demo profile identity.
- Produces:
  - `recall_demo_config_path(environ: Mapping[str, str]) -> Path`
  - `DemoRecallConfig.from_dict(value: object) -> DemoRecallConfig`
  - `load_demo_recall_config(path: Path) -> DemoRecallConfig`
  - `write_demo_recall_config(path: Path, config: DemoRecallConfig) -> None`
  - `DemoProviderConfig` containing only reader fields.
  - `DemoPublisherConfig` containing reader fields plus signing and Registry source fields.

- [ ] **Step 1: Write strict configuration REDs**

Create `tests/test_recall_demo_config.py` with class
`RecallDemoConfigTests` and these exact methods/assertions:

- `test_config_is_closed_absolute_and_owner_only`: parse the exact fixture below,
  then reject one extra field, one relative path, mode `0644`, and a config
  symlink.
- `test_reader_view_contains_no_private_key_field`: assert the reader view's
  dataclass fields are exactly `repository_name`, `product_name`,
  `decision_space_id`, `profile_path`, `model_state_root`, `trust_root_path`,
  and `bundle_state_root`.
- `test_writer_refuses_existing_or_group_readable_file`: create the path once,
  verify mode `0600`, then assert a second write raises and preserves identical
  bytes.
- `test_cli_emits_only_configured_status_not_paths`: capture stdout for
  configure/status; assert the exact 12-character profile/model prefixes occur,
  while no fixture path, signing key ID, or full digest occurs.
- `test_invalid_demo_config_is_bounded`: corrupt each identity field and assert
  CLI stderr is exactly `{"error":"recall_demo_config_invalid"}`.

Use this exact serialized shape in fixtures:

```python
{
    "schema_version": 1,
    "repository_name": "zstack-ui-next",
    "product_name": "third-party-services",
    "decision_space_id": "prod_3e6e73b8defbfee89ce7bf26e739b1dc",
    "registry_product_root": "/private/registry/product",
    "profile_path": "/private/demo-profile.json",
    "model_state_root": "/private/model-state",
    "trust_root_path": "/private/demo-public-key",
    "bundle_state_root": "/private/bundles",
    "signing_private_key_path": "/private/demo-private-key",
    "signing_key_id": "demo-leadership-v1"
}
```

- [ ] **Step 2: Run the configuration RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_demo_config -v
```

Expected: import failure for `zdecision.recall.demo.config`.

- [ ] **Step 3: Implement the closed configuration types**

Implement immutable dataclasses with exact-field parsing:

```python
@dataclass(frozen=True)
class DemoProviderConfig:
    repository_name: str
    product_name: str
    decision_space_id: str
    profile_path: Path
    model_state_root: Path
    trust_root_path: Path
    bundle_state_root: Path


@dataclass(frozen=True)
class DemoPublisherConfig:
    provider: DemoProviderConfig
    registry_product_root: Path
    signing_private_key_path: Path
    signing_key_id: str


@dataclass(frozen=True)
class DemoRecallConfig:
    schema_version: Literal[1]
    provider: DemoProviderConfig
    publisher: DemoPublisherConfig
```

Require every path to be absolute, reject symlinks for the config file, require file mode `0600`, reject unknown fields, and pin the three Demo identity strings exactly. `DemoRecallConfig.provider` must not expose `registry_product_root`, `signing_private_key_path`, or `signing_key_id`.

- [ ] **Step 4: Add bounded operator commands**

Extend `zdecision-agent` with:

```text
zdecision-agent recall-demo configure \
  --registry-product-root ABS \
  --profile ABS \
  --model-state-root ABS \
  --trust-root ABS \
  --bundle-state-root ABS \
  --signing-private-key ABS \
  --signing-key-id demo-leadership-v1

zdecision-agent recall-demo status
```

Before writing, `configure` must load the fixed profile, verify the prepared
model installation, verify that the signing private key matches the public
trust root by signing and verifying a fixed setup nonce, validate the Registry
product metadata/Decision Space, and create the bundle state root with mode
`0700`. It then creates only
`private_state_root(environ)/agent/recall-demo.json` with mode `0600` and
refuses overwrite. `status` returns one of these bounded shapes:

```json
{"status":"configured","profile_digest_prefix":"0123456789ab","model_install_digest_prefix":"abcdef012345","current_generation":null,"current_digest_prefix":null}
```

```json
{"status":"not-configured","profile_digest_prefix":null,"model_install_digest_prefix":null,"current_generation":null,"current_digest_prefix":null}
```

```json
{"status":"invalid","profile_digest_prefix":null,"model_install_digest_prefix":null,"current_generation":null,"current_digest_prefix":null}
```

When a current generation exists, `current_generation` is its positive integer
and `current_digest_prefix` is exactly the first 12 lowercase hexadecimal
characters. Neither command may print any path, key ID, full digest, Decision
content, repository ID, or exception text.

- [ ] **Step 5: Run configuration tests and existing CLI tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_config \
  tests.test_agent_service -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit configuration**

```bash
git add src/zdecision/recall/demo/config.py src/zdecision/agent/cli.py \
  tests/test_recall_demo_config.py
git commit -m "feat: configure local Recall demo state"
```

---

### Task 3: Publish immutable signed bundle generations

**Files:**
- Create: `src/zdecision/recall/demo/publication.py`
- Create: `tests/test_recall_demo_publication.py`
- Modify: `src/zdecision/recall/demo/bundle.py`
- Modify: `src/zdecision/recall/demo/index.py`
- Modify: `src/zdecision/recall/demo/retrieval.py`
- Modify: `tests/test_recall_demo_bundle.py`
- Modify: `tests/test_recall_demo_retrieval.py`

**Interfaces:**
- Consumes: `DemoPublisherConfig`, `build_signed_bundle`, `load_verified_bundle`, and prepared model-state pointer.
- Produces:
  - `DemoBundlePointer.from_dict(value: object) -> DemoBundlePointer`
  - `load_demo_bundle_pointer(config: DemoProviderConfig) -> DemoBundlePointer`
  - `DemoBundlePublisher.refresh(publication_commit: str) -> DemoBundlePointer`
  - sanitized `RecallDemoPublicationError(code: str)`.

- [ ] **Step 1: Write corpus evolution REDs**

Add tests proving the current fixed-corpus implementation is rejected by the
desired behavior:

- `test_bundle_accepts_an_eleventh_active_head`: add one complete active head,
  build and verify, and assert `len(bundle.decisions) == 11`.
- `test_bundle_accepts_positive_second_revision_head`: replace one active head
  with a complete revision 2 and assert the verified bundle contains revision 2
  and no revision 1 for that Decision ID.
- `test_bundle_ignores_non_active_heads`: add a retired head and assert it is
  absent from the verified snapshot and manifest leaves.
- `test_bundle_rejects_zero_or_more_than_thirty_two_active_heads`: build corpus
  sizes 0 and 33 and assert sanitized `DemoBundleError("source_invalid")`.

The 11th head must be a complete valid formal `DecisionRevision`; the r2 case must replace the same Decision's r1 active head rather than include both revisions.

- [ ] **Step 2: Run the dynamic-corpus REDs**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_bundle.DemoBundleTests.test_bundle_accepts_an_eleventh_active_head \
  tests.test_recall_demo_bundle.DemoBundleTests.test_bundle_accepts_positive_second_revision_head -v
```

Expected: fail at the exact-ten/revision-one checks.

- [ ] **Step 3: Relax only the frozen corpus cardinality/revision checks**

Replace `_FROZEN_DECISION_COUNT = 10` with:

```python
_MIN_ACTIVE_DECISIONS = 1
_MAX_ACTIVE_DECISIONS = 32


def _valid_active_count(value: int) -> bool:
    return _MIN_ACTIVE_DECISIONS <= value <= _MAX_ACTIVE_DECISIONS
```

Require `head.lifecycle == "active"` and `head.head_revision > 0`; include only active heads in sorted Decision-ID order. Update manifest/snapshot/index/retrieval validation to require positive revisions and matching signed leaves instead of literal revision 1. Retain every other source, signature, canonical JSON, identity, and byte-bound check.

- [ ] **Step 4: Write generation/pointer REDs**

Create `tests/test_recall_demo_publication.py` with class
`RecallDemoPublicationTests` and these exact methods/assertions:

- `test_completed_commit_builds_verifies_then_selects_generation`: record call
  order `build`, `verify`, `replace`; assert generation 1 and selected bundle.
- `test_same_commit_is_idempotent_and_does_not_resign`: call refresh twice and
  assert identical pointer plus one signing call.
- `test_existing_commit_with_different_bytes_fails_closed`: alter immutable
  generation metadata and assert `generation_conflict` without pointer change.
- `test_build_or_verify_failure_preserves_previous_pointer_bytes`: inject each
  failure and compare exact `current.json` bytes before/after.
- `test_pointer_rejects_absolute_escape_symlink_and_unknown_fields`: exercise
  all three malformed pointer shapes and assert bounded pointer error.
- `test_new_revision_changes_generation_and_manifest_digest`: publish an r2
  active head under a new commit and assert generation increments and both
  generation/manifest digests change.

Use this exact pointer schema:

```python
{
    "schema_version": 1,
    "generation": 2,
    "publication_commit": "a" * 40,
    "bundle": "bundles/" + "a" * 40 + "/bundle",
    "manifest_digest": "b" * 64,
    "profile_digest": "c" * 64,
    "model_install_digest": "d" * 64,
    "generation_digest": "e" * 64
}
```

- [ ] **Step 5: Run the publisher RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_demo_publication -v
```

Expected: import failure for `zdecision.recall.demo.publication`.

- [ ] **Step 6: Implement immutable generations and atomic selection**

Implement this public contract:

```python
DemoBundlePublisher(config: DemoPublisherConfig)
DemoBundlePublisher.refresh(publication_commit: str) -> DemoBundlePointer
```

Rules in code:

1. `publication_commit` must be exactly 40 lowercase hexadecimal characters.
2. Generation directory is `bundle_state_root / "bundles" / publication_commit`.
3. Signed bundle lives in its child `bundle/`; immutable metadata lives in `generation.json`.
4. Build into an owner-only sibling staging directory, verify the signed bundle using the public trust root, verify the prepared model pointer/manifest, fsync, then rename the complete generation with no replacement.
5. If the exact commit directory exists, verify its bundle and metadata and reuse it without signing again.
6. Any mismatch raises `RecallDemoPublicationError("generation_conflict")`.
7. Assign `generation = previous.generation + 1`, or 1 when there is no prior pointer.
8. Derive `generation_digest` from canonical JSON of all pointer fields except `generation_digest`.
9. Write an owner-only pointer candidate and `os.replace` it as `current.json` only after all verification succeeds.
10. On any failure, leave the previous `current.json` byte-for-byte unchanged and emit only a sanitized error code.

- [ ] **Step 7: Run bundle/publisher/retrieval suites**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_bundle \
  tests.test_recall_demo_publication \
  tests.test_recall_demo_retrieval -v
```

Expected: all tests pass, including 1/11/32/r2 corpus cases and previous prototype security tests.

- [ ] **Step 8: Commit immutable publication**

```bash
git add src/zdecision/recall/demo/bundle.py \
  src/zdecision/recall/demo/index.py \
  src/zdecision/recall/demo/retrieval.py \
  src/zdecision/recall/demo/publication.py \
  tests/test_recall_demo_bundle.py tests/test_recall_demo_retrieval.py \
  tests/test_recall_demo_publication.py
git commit -m "feat: publish immutable Recall demo generations"
```

---

### Task 4: Implement `DemoRecallProvider`

**Files:**
- Create: `src/zdecision/recall/demo/provider.py`
- Create: `tests/test_recall_demo_provider.py`
- Modify: `src/zdecision/recall/demo/bundle.py`
- Modify: `src/zdecision/recall/demo/model_store.py`

**Interfaces:**
- Consumes: `DemoProviderConfig`, `DemoBundlePointer`, existing `RecallProvider`, `RecallPreflightReady`, `RecallPreflightClarification`, `RecallPreflightUnavailable`, `RecallShortlist`, and promoted retriever/runtime.
- Produces:
  - `DemoRecallProvider.preflight() -> RecallPreflightResult`
  - `DemoRecallProvider.retrieve(preflight: RecallPreflightReady) -> RecallShortlist`
  - `configured_recall_provider(path: Path) -> RecallProvider` which returns `UnavailableRecallProvider` on any absent/invalid state.

- [ ] **Step 1: Write provider REDs**

Create `tests/test_recall_demo_provider.py` with class
`RecallDemoProviderTests` and these exact methods/assertions:

- `test_exact_product_preflight_freezes_selected_generation`: assert every
  `RecallPreflightReady` digest/generation field equals the selected pointer.
- `test_ambiguous_intent_returns_only_third_party_services_display_name`:
  assert the result is `RecallPreflightClarification` with exactly one bounded
  display name and no provider path or Decision text.
- `test_wrong_repository_or_product_is_unavailable_or_clarification`: wrong
  repository is unavailable; absent product selection is clarification; an
  explicit different product is unavailable.
- `test_preflight_does_not_import_torch_load_runtime_or_retrieve`: patch these
  operations to raise and assert ready preflight still succeeds.
- `test_preflight_emits_no_decision_text_or_private_path`: serialize result and
  assert all fixture prose/path/key sentinels are absent.
- `test_retrieve_maps_ranked_items_to_complete_recalled_decisions`: assert full
  formal revisions, canonical digests, and deterministic match reasons.
- `test_retrieve_rejects_pointer_bundle_profile_or_model_generation_change`:
  mutate each binding independently and assert sanitized provider unavailable.
- `test_empty_ranked_result_returns_a_valid_empty_shortlist`: assert an empty
  tuple and matching preflight digest.
- `test_runtime_index_cache_is_keyed_by_exact_generation_digest`: same key loads
  once; changed generation builds a separate entry.
- `test_invalid_or_missing_config_returns_unavailable_provider`: assert exact
  `UnavailableRecallProvider` type for both cases.

Use a `RecallIntent` whose `target_decision_space_ids` is exactly `("prod_3e6e73b8defbfee89ce7bf26e739b1dc",)` and whose path is `packages/products/third-party-services/apps/security-services`.

- [ ] **Step 2: Run provider REDs**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_demo_provider -v
```

Expected: import failure for `zdecision.recall.demo.provider`.

- [ ] **Step 3: Add metadata-only verification helpers**

Add bounded helpers that do not import ML libraries:

```python
load_verified_bundle_metadata(
    *, bundle_root: Path, trust_root_path: Path
) -> VerifiedDemoBundleMetadata

load_installed_model_metadata(
    profile: DemoRetrievalProfile, state_root: Path
) -> InstalledModelMetadata
```

The bundle metadata helper verifies the signed manifest, profile, file bindings, Decision count/leaves, and canonical payload digests but does not parse Decision prose into Hook output. The model metadata helper verifies the owner-only current pointer and install manifest identity/digests but does not import Torch or hash/load model tensors. Full `load_verified_bundle` and `load_installed_models` remain mandatory in `retrieve()`.

- [ ] **Step 4: Implement provider preflight**

Implement `DemoRecallProvider.preflight`, with keyword-only
`repository_id: str`, `repository_display_name: str`, `intent: RecallIntent`,
and `now: datetime`, returning `RecallPreflightResult`. Implement
`DemoRecallProvider.retrieve(preflight: RecallPreflightReady) -> RecallShortlist`.

`preflight()` must:

- require `repository_display_name == "zstack-ui-next"`;
- return clarification with only `("third-party-services",)` when target selection is absent/ambiguous;
- require the exact Decision Space when selection is explicit;
- validate current pointer, signed metadata, profile metadata, and prepared model metadata;
- create `RecallPreflightReady` with:
  - `catalog_digest = pointer.manifest_digest`;
  - `generation = pointer.generation`;
  - `generation_digest = pointer.generation_digest`;
  - `retrieval_profile_digest = pointer.profile_digest`;
  - `index_generation = pointer.generation`;
  - `freshness = "ready"`;
  - `expires_at = now + 15 minutes` in canonical UTC form;
- collapse every local validation/runtime exception to `RecallPreflightUnavailable(code="recall_not_ready")`.

- [ ] **Step 5: Implement provider retrieve and cache**

`retrieve()` must re-run all exact pointer/generation/profile/model checks, then call full bundle/model/runtime/index/retriever paths. Map results exactly:

```python
items = tuple(
    RecalledDecision.create(
        decision_space_id=preflight.target_decision_space_ids[0],
        revision=item.revision,
        match_reason=item.match_reason,
    )
    for item in result.items
)
return RecallShortlist.create(preflight=preflight, items=items)
```

Before returning, assert each generated `RecalledDecision.digest == item.digest`; otherwise raise `RecallProviderUnavailable("Recall provider is unavailable")`. Cache the verified runtime/index only under a tuple of exact `generation_digest`, `profile_digest`, `model_install_digest`, and `manifest_digest`; never mutate a cache entry.

- [ ] **Step 6: Run provider and handoff contracts**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_provider \
  tests.test_recall_handoff_contracts \
  tests.test_recall_handoff_service -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit provider**

```bash
git add src/zdecision/recall/demo/provider.py \
  src/zdecision/recall/demo/bundle.py \
  src/zdecision/recall/demo/model_store.py \
  tests/test_recall_demo_provider.py
git commit -m "feat: add local Recall demo provider"
```

---

### Task 5: Wire one provider into Hook and MCP startup

**Files:**
- Modify: `src/zdecision/agent/cli.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_mcp_recall_handoff.py`
- Modify: `tests/test_hook_latency.py`
- Create: `tests/test_recall_demo_wiring.py`

**Interfaces:**
- Consumes: `configured_recall_provider(recall_demo_config_path(environ))`.
- Produces: identical provider selection for the Hook process and the MCP process; no public MCP schema changes.

- [ ] **Step 1: Write wiring REDs**

Create `tests/test_recall_demo_wiring.py` with class
`RecallDemoWiringTests` and these exact methods/assertions:

- `test_hook_and_mcp_load_provider_from_same_config_path`: patch the provider
  factory, invoke both CLI paths, and assert two calls with the same resolved
  owner-only path.
- `test_missing_config_keeps_unavailable_provider_in_both_processes`: invoke
  both paths without the file and assert bounded `recall_not_ready` behavior.
- `test_invalid_config_never_falls_back_to_private_argument_or_environment`:
  populate tempting environment/path values, corrupt the configured file, and
  assert none are read or selected.

Add Hook latency tests that patch all of these to fail if called during preflight:

```python
patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
patch("builtins.__import__", guarded_import_that_rejects_torch_and_transformers)
patch("zdecision.recall.demo.runtime.load_transformers_runtime", side_effect=AssertionError)
```

Retain the existing p95 bound of 150 ms.

- [ ] **Step 2: Run wiring REDs**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_wiring \
  tests.test_hook_latency -v
```

Expected: Hook/MCP still instantiate `UnavailableRecallProvider`; wiring test fails.

- [ ] **Step 3: Inject provider in Hook CLI**

Change the existing Hook call to:

```python
provider = configured_recall_provider(recall_demo_config_path(os.environ))
response = handle_hook(
    raw,
    database=database,
    clock=lambda: datetime.now(UTC),
    recall_provider=provider,
)
```

Do not add environment-variable overrides or tool/model parameters for config paths.

- [ ] **Step 4: Inject provider in MCP startup**

Extend `run_mcp` with one explicit `recall_demo_config_path: Path` argument supplied by the CLI's same well-known path. Construct the provider before `RecallHandoffService`:

```python
provider = configured_recall_provider(recall_demo_config_path)
handoff = RecallHandoffService(
    store=recall_store,
    provider=provider,
    clock=recall_clock,
    delivery_id_factory=delivery_id_for_attempt,
    claim_token_factory=lambda: f"claim_{uuid4().hex}",
)
```

Remove only the local `UnavailableRecallProvider()` construction. Leave all Recall tools, UI resources, app-only visibility, Hook trusted coordinate rewriting, and application state unchanged.

- [ ] **Step 5: Prove the trusted card boundary with the Demo provider**

Extend `tests/test_recall_hook_gate.py` and `tests/test_mcp_recall_handoff.py` to prove:

- Hook preflight creates a pending activation attempt using Demo generation digests;
- no `retrieve()` call occurs before trusted card consent;
- enable invokes retrieve exactly once outside the SQLite transaction;
- double-click/replay does not retrieve twice;
- changing `current.json` between Hook preflight and enable returns bounded delivery failure and does not inject context;
- missing config retains `recall_not_ready` behavior.

- [ ] **Step 6: Run Hook/MCP focused verification**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_wiring \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_handoff \
  tests.test_recall_handoff_service \
  tests.test_hook_latency -v
```

Expected: all tests pass; Hook ready-preflight p95 remains at or below 150 ms.

- [ ] **Step 7: Commit runtime wiring**

```bash
git add src/zdecision/agent/cli.py src/zdecision/agent/mcp_server.py \
  tests/test_recall_demo_wiring.py tests/test_recall_hook_gate.py \
  tests/test_mcp_recall_handoff.py tests/test_hook_latency.py
git commit -m "feat: wire Demo provider into Recall"
```

---

### Task 6: Refresh Demo Recall after Central publication

**Files:**
- Modify: `src/zdecision/central/web/application.py`
- Modify: `src/zdecision/central/cli.py`
- Modify: `src/zdecision/central/api.py`
- Create: `tests/test_recall_demo_publication_bridge.py`
- Modify: `tests/test_central_web_api.py`
- Modify: `tests/test_central_web_review.py`

**Interfaces:**
- Consumes: optional `DemoBundlePublisher` and completed `CentralPublication.commit_sha`.
- Produces: publication-coupled automatic Demo refresh and bounded `RecallDemoRefreshFailed` API result.

- [ ] **Step 1: Write Central bridge REDs**

Create `tests/test_recall_demo_publication_bridge.py` with class
`RecallDemoPublicationBridgeTests` and these exact methods/assertions:

- `test_completed_publication_refreshes_after_registry_projection`: assert the
  two-call order shown below.
- `test_pending_or_failed_publication_never_refreshes`: pass both states and
  assert zero publisher calls.
- `test_unconfigured_central_behavior_is_unchanged`: compare the existing
  publication result with publisher `None`.
- `test_refresh_failure_returns_bounded_error_and_preserves_pointer`: inject
  `RecallDemoPublicationError`, assert the exact API response, and compare
  pointer bytes.
- `test_retry_completed_publication_refreshes_without_second_publish_commit`:
  assert two refresh attempts, one Git publication commit, and one immutable
  generation build.

The fake synchronizer and fake publisher must record call order and assert:

```python
self.assertEqual(calls, [
    ("registry", publication.commit_sha),
    ("recall-demo", publication.commit_sha),
])
```

- [ ] **Step 2: Run the Central bridge RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_demo_publication_bridge -v
```

Expected: `CentralWebApplication` rejects the new publisher dependency or never calls it.

- [ ] **Step 3: Add the optional publisher boundary**

Extend constructor shape exactly:

```python
def __init__(
    self,
    *,
    store: CentralWebStore,
    queries: CentralWebQueries,
    catalog: RegistryCatalog | None = None,
    git: GitRegistryAdapter | None = None,
    registry_synchronizer: RegistryProjectionSynchronizer | None = None,
    recall_demo_publisher: DemoBundlePublisher | None = None,
) -> None:
```

After successful Registry projection, call:

```python
if self.recall_demo_publisher is not None:
    try:
        self.recall_demo_publisher.refresh(publication.commit_sha)
    except RecallDemoPublicationError:
        raise RecallDemoRefreshFailed("recall_demo_refresh_failed") from None
```

Do not run the publisher when the publication is not completed or the Registry projection failed.

- [ ] **Step 4: Map refresh failure at the HTTP boundary**

Add a single API exception handler returning HTTP 503 and exact body:

```json
{"error":"recall_demo_refresh_failed"}
```

Do not include a path, commit, key, digest, exception, or traceback in the response.

- [ ] **Step 5: Construct the optional publisher in Central CLI**

At `_run_server`, load the same well-known owner-only Demo config. Behavior must be:

```python
try:
    demo_config = load_demo_recall_config(recall_demo_config_path(os.environ))
except FileNotFoundError:
    recall_demo_publisher = None
except (OSError, ValueError):
    raise CentralCliError("recall_demo_config_invalid") from None
else:
    recall_demo_publisher = DemoBundlePublisher(demo_config.publisher)
```

Pass this optional publisher to `CentralWebApplication`. Do not let Central silently start with invalid configured Demo state.

- [ ] **Step 6: Prove idempotent retry semantics**

Add a test where publication is already `completed`: invoking the existing resume/retry application boundary must call publisher refresh again with the same commit while the fake Git publication commit count remains exactly one. The publisher's own same-commit test must prove no second signature/build occurs.

- [ ] **Step 7: Run Central publication/API suites**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_publication_bridge \
  tests.test_central_web_api \
  tests.test_central_web_review \
  tests.test_registry_projection -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit Central bridge**

```bash
git add src/zdecision/central/web/application.py \
  src/zdecision/central/cli.py src/zdecision/central/api.py \
  tests/test_recall_demo_publication_bridge.py \
  tests/test_central_web_api.py tests/test_central_web_review.py
git commit -m "feat: refresh Recall demo after publication"
```

---

### Task 7: Prove the leadership-demo vertical and document the rehearsal

**Files:**
- Create: `tests/integration/test_recall_demo_provider_bridge.py`
- Create: `docs/demo-recall-provider.md`
- Modify: `plugins/zdecision/.codex-plugin/plugin.json`
- Modify: `plugins/zdecision/skills/zdecision/SKILL.md`
- Modify: `tests/test_plugin_contract.py`
- Modify: `tests/test_recall_skill_contract.py`

**Interfaces:**
- Consumes: Tasks 1–6 and all existing Candidate, Central, Gate A, and application boundaries.
- Produces: automated publish-to-recall proof, honest plugin wording, and one exact leadership rehearsal.

- [ ] **Step 1: Write the end-to-end RED**

Create class `RecallDemoProviderBridgeIntegrationTests` with method
`test_unpublished_candidate_is_absent_then_published_decision_is_recalled`.
Use real filesystem bundles and production boundary classes, but inject the
existing deterministic embedding/reranker fakes from the promoted retrieval
tests. The test must perform and assert this exact sequence:

1. Build/select generation N from the current active Registry fixture and
   assert the candidate-only Decision ID is absent.
2. Use `CentralWebApplication.publish` to complete the candidate's formal
   publication and capture its commit SHA.
3. Read `DemoBundlePublisher`'s selected pointer and assert generation N+1 plus
   matching publication commit.
4. Create a fresh Demo provider preflight with the exact repository/product
   intent and assert it freezes generation N+1.
5. Use `RecallHandoffService.enable` after a trusted activation attempt and
   assert retrieve count is one and the new Decision ID is present.
6. ACK delivery, create the next native gate through the Hook path, submit
   complete `applicable`/`not_applicable` classifications through the MCP tool,
   and assert `application_committed` plus mutation allowed.

Also assert Candidate/Capture state, production Registry files outside the test fixture, and any pre-existing bundle pointer are unchanged.

- [ ] **Step 2: Run the integration RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_recall_demo_provider_bridge -v
```

Expected: fail at the first missing bridge behavior, not at test fixture setup.

- [ ] **Step 3: Complete only integration glue exposed by the RED**

Use the production `DemoBundlePublisher`, `DemoRecallProvider`, `RecallHandoffService`, `RecallMcpTools`, `RecallHostStore`, and Central application. Do not add a test-only provider path to production and do not bypass Hook/MCP trusted coordinates with model-authored IDs.

- [ ] **Step 4: Update Plugin wording honestly**

Set plugin version exactly to:

```json
"version": "0.1.0+codex.20260813190000"
```

Replace only the obsolete provider warning with:

```text
When the local third-party-services leadership Demo is configured and its signed bundle is current, Recall can retrieve that Demo corpus. Other repositories, products, missing Demo state, or invalid generations remain unavailable. This does not claim production Gate B/C readiness.
```

In the Recall Skill, retain all card/application ordering and replace the final production-unavailable bullet with the same bounded Demo truth. Do not make invocation implicit.

- [ ] **Step 5: Write the exact leadership runbook**

`docs/demo-recall-provider.md` must contain these sections and commands with concrete values:

1. Install optional dependencies:

   ```bash
   .venv/bin/python -m pip install -e '.[recall-demo]'
   ```

2. Prepare the two pinned models once using `zdecision-recall-demo prepare-models`.
3. Generate/store the external Ed25519 private/public keys with owner-only permissions.
4. Run `zdecision-agent recall-demo configure` with the Registry product root for `prod_3e6e73b8defbfee89ce7bf26e739b1dc`.
5. Publish the current Registry commit once to seed generation 1.
6. Start Central and the existing Agent service.
7. Reinstall/reload the local ZDecision plugin and trust its eight Hook entries if the bundle hash changed.
8. Leadership flow:
   - select ZDecision Candidate refresh and send `更新候选决策`;
   - click the Candidate card's Decision Center action;
   - review and publish the security-services Candidate;
   - verify Central reports publication completed and Demo refresh succeeded;
   - open a new `zstack-ui-next` task and select ZDecision;
   - state `third-party-services` and `packages/products/third-party-services/apps/security-services`;
   - click the Recall card;
   - keep the attachment and send the next native message;
   - verify the newly published Decision appears in the complete handoff and application is `application_committed`;
   - stop before code modification.
9. Failure demo: remove/rename the config and show `recall_not_ready`, with no fabricated Decision.
10. Rollback: restore prior plugin installation and remove only the exact Demo config/bundle/model paths named during setup; never delete broad state roots.

- [ ] **Step 6: Run all focused Demo and Gate A suites**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_demo_bundle \
  tests.test_recall_demo_models \
  tests.test_recall_demo_projection \
  tests.test_recall_demo_retrieval \
  tests.test_recall_demo_cli \
  tests.test_recall_demo_config \
  tests.test_recall_demo_publication \
  tests.test_recall_demo_provider \
  tests.test_recall_demo_wiring \
  tests.test_recall_demo_publication_bridge \
  tests.test_recall_hook_gate \
  tests.test_recall_handoff_service \
  tests.test_mcp_recall_handoff \
  tests.test_mcp_recall_confirmation \
  tests.integration.test_inline_candidate_refresh \
  tests.test_requested_capture \
  tests.test_recall_capture_isolation \
  tests.test_cli_review_publish \
  tests.test_browser_launcher \
  tests.test_plugin_contract \
  tests.test_recall_skill_contract \
  tests.integration.test_recall_demo_provider_bridge -v
```

Expected: all tests pass.

- [ ] **Step 7: Run static and real-model verification**

Run:

```bash
.venv/bin/python -m compileall -q src/zdecision tests
.venv/bin/python -m json.tool plugins/zdecision/.codex-plugin/plugin.json >/dev/null
git diff --check
git status --short
```

Then, only on the prepared offline demo machine:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
ZDECISION_RUN_REAL_MODEL_SMOKE=1 \
.venv/bin/python -m unittest \
  tests.test_recall_demo_models.RealRuntimeSmokeTests -v
```

Expected: no network access; real model smoke passes using prepared local snapshots. `git status --short` still lists only intended tracked changes plus the three protected pre-existing untracked files.

- [ ] **Step 8: Commit the completed Demo vertical**

```bash
git add tests/integration/test_recall_demo_provider_bridge.py \
  docs/demo-recall-provider.md \
  plugins/zdecision/.codex-plugin/plugin.json \
  plugins/zdecision/skills/zdecision/SKILL.md \
  tests/test_plugin_contract.py tests/test_recall_skill_contract.py
git commit -m "feat: complete Recall leadership demo bridge"
```

- [ ] **Step 9: Perform one real Desktop rehearsal without code changes**

Follow `docs/demo-recall-provider.md` exactly. Record only bounded evidence:

- publication state and publication commit prefix;
- selected bundle generation number and digest prefix;
- Recall card states `pending_confirmation` then `host_delivered`;
- count of recalled Decisions and whether the newly published Decision ID prefix is present;
- application state `application_committed`;
- mutation guard released;
- no code changed and no network was used during Recall.

Do not record full Decisions, private paths, keys, session/turn IDs, raw database rows, or model scores in the acceptance note.

---

## Final Verification Gate

Before claiming the Demo complete:

1. Confirm all seven task commits exist and each task's exact focused suite passed after its commit.
2. Run the Task 7 focused command once from a clean tracked worktree.
3. Run `git diff --check` and `git status --short`.
4. Confirm the three protected pre-existing untracked files remain untouched and unstaged.
5. Confirm a newly published formal Decision is absent before publication, present in the new signed generation after publication, recalled after trusted consent, and atomically applied.
6. Confirm missing/invalid config, generation mismatch, model mismatch, signature mismatch, and refresh failure all fail closed with bounded codes.
7. Confirm no statement or UI copy calls this Gate B/C, production distribution, or representative retrieval quality.
