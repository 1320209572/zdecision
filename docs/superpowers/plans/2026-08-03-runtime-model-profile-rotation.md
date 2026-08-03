# Runtime Model Profile Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one stable local extraction model while it remains supported,
rotate it safely when Codex removes it, freeze one exact profile per Capture
Request, and resume the currently failed real request without producing an
unexpected processor error.

**Architecture:** The Agent database keeps the current active profile as a
compare-and-swap record. The app-server Gateway treats the full model catalog
as discovery evidence rather than permanent identity: it reuses an active
model/effort pair that remains supported and selects the app-server default
only when rotation is required. The private Session Index freezes the resolved
profile once per non-empty request; every source operation copies that profile,
and retries validate and reuse it without substitution.

**Tech Stack:** Python 3.14, stdlib `sqlite3`, Codex app-server JSONL,
`unittest`, existing ZDecision local Agent and FastAPI technical-demo service.

## Global Constraints

- Work directly on `main`; do not create a worktree or a feature branch.
- A request with no changed Session source performs no model discovery and
  completes with zero Candidates.
- A catalog digest change alone never invalidates a supported active
  `model_id` plus reasoning effort.
- Rotation may select only the single default model and its explicit default
  reasoning effort returned by validated app-server discovery; never guess a
  model slug.
- Resolve and persist one profile before creating any source operation for a
  request; all source operations in that request must use that profile ID.
- Existing operations and disposable retry generations always reuse their
  frozen profile. An unsupported frozen profile fails as
  `frozen_model_unavailable`; it is never silently replaced.
- Raw Session content, Prompts, source code, diffs, and app-server Thread data
  remain local and never enter central storage.
- Do not change inline button rendering, Hook trust, Candidate Review,
  publication, recall, or any Gate outside the model-profile defect.
- The live central service remains stopped until focused tests and a read-only
  app-server preflight pass, because request
  `crq_407361107583b1b276f27e709fd41762` has only one retry left.

---

## File Map

- `src/zdecision/agent/db.py`: active-profile compare-and-swap persistence.
- `src/zdecision/app_server/gateway.py`: catalog validation, supported-profile
  reuse, default-profile rotation, and frozen-profile support checks.
- `src/zdecision/agent/session_index.py`: private request-profile freeze and
  nullable migration for pre-amendment requests.
- `src/zdecision/app_server/requested_capture.py`: expose existing operation
  profiles, resolve/validate request profiles, and run with a supplied frozen
  profile.
- `src/zdecision/agent/capture_processor.py`: resolve one request profile,
  reject mixed replay, skip empty requests, and map frozen-model failure.
- `tests/test_app_server_gateway.py`: active-profile reuse, rotation, and CAS
  behavior.
- `tests/test_session_index.py`: request-profile persistence, replay, conflict,
  and old-schema migration.
- `tests/test_requested_capture.py`: supplied-profile operation identity and
  retry behavior.
- `tests/test_capture_request_processor.py`: request-level orchestration and
  explicit failure mapping.
- `tests/integration/test_on_demand_capture_core.py`: one-profile multi-source
  and crash/retry integration coverage.

---

### Task 1: Resolve and Rotate the Active Profile

**Files:**
- Modify: `src/zdecision/agent/db.py`
- Modify: `src/zdecision/app_server/gateway.py`
- Test: `tests/test_app_server_gateway.py`

**Interfaces:**
- Produces:
  `AgentDatabase.activate_feasibility_model_profile(*,
  expected_profile_id: str | None, profile_id: str, model_id: str,
  reasoning_effort: str, discovery_digest: str, discovered_at: str)
  -> StoredFeasibilityModelProfile`.
- Produces:
  `AppServerGateway.resolve_active_profile() -> FeasibilityModelProfile`.
- Produces:
  `AppServerGateway.require_supported_profile(profile:
  FeasibilityModelProfile) -> FeasibilityModelProfile`.
- Removes the runtime use of `ModelDiscoveryConflict`; a changed catalog is
  resolved through support checking and bounded CAS instead.

- [ ] **Step 1: Replace the obsolete conflict test with supported reuse tests**

Extend `model_catalog` with `extra_model: str | None = None` and
`omit_model: str | None = None`. An extra model is appended as a non-default
entry supporting `medium`; an omitted ID is filtered from `data`. Then add:

```python
def test_catalog_change_reuses_still_supported_active_profile(self):
    first_catalog = model_catalog()
    changed_catalog = model_catalog(extra_model="model-added")
    gateway = self._gateway(ScriptedClient([first_catalog, changed_catalog]))

    first = gateway.resolve_active_profile()
    replay = gateway.resolve_active_profile()

    self.assertEqual(first, replay)
    self.assertNotEqual(
        hashlib.sha256(canonical_json_bytes({"models": first_catalog["data"]})).hexdigest(),
        hashlib.sha256(canonical_json_bytes({"models": changed_catalog["data"]})).hexdigest(),
    )


def test_removed_active_profile_rotates_to_returned_default(self):
    gateway = self._gateway(
        ScriptedClient([
            model_catalog(default_model="model-old"),
            model_catalog(default_model="model-new", omit_model="model-old"),
        ])
    )

    old = gateway.resolve_active_profile()
    new = gateway.resolve_active_profile()

    self.assertEqual("model-old", old.model_id)
    self.assertEqual("model-new", new.model_id)
    self.assertNotEqual(old.profile_id, new.profile_id)
    self.assertEqual(new.profile_id, self.database.get_feasibility_model_profile().profile_id)
```

Add a CAS test using two `AgentDatabase` connections to the same temporary
SQLite file:

```python
def test_stale_profile_rotation_returns_the_cas_winner(self):
    other = AgentDatabase.open(self.root / "agent.sqlite3")
    self.addCleanup(other.close)
    old = activate_profile(self.database, None, "model-old", "a" * 64)
    stale_expected = old.profile_id
    winner = activate_profile(
        self.database, stale_expected, "model-winner", "b" * 64
    )

    stale_result = activate_profile(
        other, stale_expected, "model-loser", "c" * 64
    )

    self.assertEqual(winner, stale_result)
    self.assertEqual(
        winner, self.database.get_feasibility_model_profile()
    )
```

The local `activate_profile` test helper creates a
`FeasibilityModelProfile`, then passes its five fields unchanged to the
production CAS method.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_app_server_gateway.AppServerGatewayTests.test_catalog_change_reuses_still_supported_active_profile \
  tests.test_app_server_gateway.AppServerGatewayTests.test_removed_active_profile_rotates_to_returned_default \
  -v
```

Expected: FAIL because `resolve_active_profile` and rotation semantics do not
exist and the old code raises `ModelDiscoveryConflict`.

- [ ] **Step 3: Add transactional active-profile CAS**

Implement `activate_feasibility_model_profile` with `BEGIN IMMEDIATE`:

```python
def activate_feasibility_model_profile(
    self,
    *,
    expected_profile_id: str | None,
    profile_id: str,
    model_id: str,
    reasoning_effort: str,
    discovery_digest: str,
    discovered_at: str,
) -> StoredFeasibilityModelProfile:
    if _MODEL_PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError("profile_id is invalid")
    if not model_id or not reasoning_effort:
        raise ValueError("model profile fields are invalid")
    if _DIGEST.fullmatch(discovery_digest) is None:
        raise ValueError("discovery_digest is invalid")
    _parse_datetime(discovered_at)
    cursor = self._connection.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        row = cursor.execute(
            "SELECT profile_id, model_id, reasoning_effort, "
            "discovery_digest, discovered_at "
            "FROM feasibility_model_profile WHERE singleton_id = 1"
        ).fetchone()
        current = None if row is None else StoredFeasibilityModelProfile(
            profile_id=row["profile_id"],
            model_id=row["model_id"],
            reasoning_effort=row["reasoning_effort"],
            discovery_digest=row["discovery_digest"],
            discovered_at=row["discovered_at"],
        )
        current_id = None if current is None else current.profile_id
        if current_id != expected_profile_id:
            self._connection.commit()
            if current is None:
                raise RuntimeError("Active model profile disappeared")
            return current
        cursor.execute(
            """
            INSERT INTO feasibility_model_profile(
                singleton_id, profile_id, model_id, reasoning_effort,
                discovery_digest, discovered_at
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                profile_id = excluded.profile_id,
                model_id = excluded.model_id,
                reasoning_effort = excluded.reasoning_effort,
                discovery_digest = excluded.discovery_digest,
                discovered_at = excluded.discovered_at
            """,
            (profile_id, model_id, reasoning_effort,
             discovery_digest, discovered_at),
        )
        self._connection.commit()
    except Exception:
        self._connection.rollback()
        raise
    finally:
        cursor.close()
    stored = self.get_feasibility_model_profile()
    if stored is None:
        raise RuntimeError("Active model profile could not be read back")
    return stored
```

Keep `get_feasibility_model_profile()` as the read API. Remove
`FeasibilityModelProfileConflict` only after all callers and tests stop using
it.

- [ ] **Step 4: Implement bounded Gateway resolution**

Add private conversions and support checking:

```python
def _profile_supported(catalog, profile) -> bool:
    model = next((item for item in catalog if item["id"] == profile.model_id), None)
    if model is None:
        return False
    return profile.reasoning_effort in {
        item["reasoningEffort"] for item in model["supportedReasoningEfforts"]
    }
```

`resolve_active_profile()` must:

1. call validated `_discover_models()` once;
2. return the stored profile when `_profile_supported` is true, regardless of
   the new catalog digest;
3. otherwise build a proposal from the catalog's sole default model and its
   validated `defaultReasoningEffort`;
4. call the database CAS with the observed active profile ID;
5. return the winner when supported by this catalog;
6. raise typed `ActiveModelProfileResolutionConflict` when a concurrent winner
   is not supported by the already observed catalog. The Capture layer maps
   this to a retryable request so the next attempt performs fresh discovery;
   the stale caller must not immediately overwrite the winner.

`require_supported_profile(profile)` performs a fresh validated discovery and
returns the identical object only when supported; otherwise it raises a new
typed `FrozenModelProfileUnavailable` Gateway error.

- [ ] **Step 5: Run focused Gateway tests and verify GREEN**

Run:

```bash
.venv/bin/python -m unittest tests.test_app_server_gateway -v
```

Expected: all Gateway and transport tests PASS; no test expects a catalog
digest change to stop the runtime.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/zdecision/agent/db.py \
  src/zdecision/app_server/gateway.py \
  tests/test_app_server_gateway.py
git commit -m "fix: rotate active capture model profile"
```

---

### Task 2: Freeze One Profile per Capture Request

**Files:**
- Modify: `src/zdecision/agent/session_index.py`
- Modify: `src/zdecision/app_server/requested_capture.py`
- Modify: `src/zdecision/agent/capture_processor.py`
- Test: `tests/test_session_index.py`
- Test: `tests/test_requested_capture.py`
- Test: `tests/test_capture_request_processor.py`

**Interfaces:**
- Consumes: `AppServerGateway.resolve_active_profile()` and
  `AppServerGateway.require_supported_profile(profile)` from Task 1.
- Produces:
  `SessionIndex.request_model_profile(request_id: str)
  -> FeasibilityModelProfile | None`.
- Produces:
  `SessionIndex.freeze_request_model_profile(request_id: str,
  profile: FeasibilityModelProfile) -> FeasibilityModelProfile`.
- Produces typed `RequestModelProfileConflict` and
  `RequestModelProfileCorrupt` errors so invalid private replay never falls
  through to `unexpected_processor_error`.
- Produces:
  `RequestedCaptureRunner.operation_profile(source: FrozenSessionSource)
  -> FeasibilityModelProfile | None`.
- Produces:
  `RequestedCaptureRunner.resolve_request_profile(profile:
  FeasibilityModelProfile | None) -> FeasibilityModelProfile`.
- Changes `RequestedCaptureRunner.run(...)` to require keyword argument
  `model_profile: FeasibilityModelProfile`.

- [ ] **Step 1: Write failing Session Index profile-freeze tests**

Add this helper and cover all four persistence rules:

```python
def model_profile(model_id: str, digest: str) -> FeasibilityModelProfile:
    return FeasibilityModelProfile.create(
        model_id=model_id,
        reasoning_effort="medium",
        discovery_digest=digest,
        discovered_at="2026-08-03T02:00:00.000000Z",
    )


def test_request_profile_freezes_once_and_survives_restart(self):
    self.index.freeze_sources(
        FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
        capture_scope="all_valid_sessions",
    )
    profile = model_profile("model-a", "a" * 64)
    self.assertEqual(profile, self.index.freeze_request_model_profile(FIRST_REQUEST_ID, profile))
    self.index.close()
    self.index = SessionIndex.open(self.database_path)
    self.assertEqual(profile, self.index.request_model_profile(FIRST_REQUEST_ID))


def test_request_profile_replay_rejects_a_different_profile(self):
    self.index.freeze_sources(
        FIRST_REQUEST_ID, REPOSITORY_ID, NOW,
        capture_scope="all_valid_sessions",
    )
    self.index.freeze_request_model_profile(
        FIRST_REQUEST_ID, model_profile("model-a", "a" * 64)
    )
    with self.assertRaisesRegex(RequestModelProfileConflict, "profile conflicts"):
        self.index.freeze_request_model_profile(
            FIRST_REQUEST_ID, model_profile("model-b", "b" * 64)
        )


def test_old_request_freeze_migrates_with_null_profile(self):
    self.index.close()
    with sqlite3.connect(self.database_path) as connection:
        connection.execute("DROP TABLE capture_request_freezes")
        connection.execute(
            """
            CREATE TABLE capture_request_freezes (
                request_id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                capture_scope TEXT NOT NULL,
                selected_session_id TEXT,
                frozen_at TEXT NOT NULL,
                acknowledged_at TEXT,
                acknowledgement_digest TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO capture_request_freezes(
                request_id, repository_id, capture_scope,
                selected_session_id, frozen_at,
                acknowledged_at, acknowledgement_digest
            ) VALUES (?, ?, 'all_valid_sessions', NULL, ?, NULL, NULL)
            """,
            (FIRST_REQUEST_ID, REPOSITORY_ID, NOW.isoformat()),
        )
    self.index = SessionIndex.open(self.database_path)
    self.assertIsNone(self.index.request_model_profile(FIRST_REQUEST_ID))


def test_profile_cannot_be_stored_for_an_unknown_request(self):
    with self.assertRaises(RequestModelProfileConflict):
        self.index.freeze_request_model_profile(
            FIRST_REQUEST_ID, model_profile("model-a", "a" * 64)
        )
```

Store canonical profile JSON in a nullable `model_profile_json` column. Parsing
must require exactly `profile_id`, `model_id`, `reasoning_effort`,
`discovery_digest`, and `discovered_at`, then reconstruct and validate a
`FeasibilityModelProfile`.

- [ ] **Step 2: Run Session Index tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_session_index -v
```

Expected: the new tests FAIL because the column and freeze APIs are absent.

- [ ] **Step 3: Implement the nullable migration and immutable request freeze**

During `SessionIndex.open`, add only this backward-compatible migration:

```sql
ALTER TABLE capture_request_freezes ADD COLUMN model_profile_json TEXT;
```

`freeze_request_model_profile` must use `BEGIN IMMEDIATE`, require an existing
request freeze, write canonical JSON only when the column is `NULL`, and reject
a byte-different replay with `RequestModelProfileConflict`.
`request_model_profile` must fail closed on malformed or non-canonical stored
JSON with `RequestModelProfileCorrupt`.

- [ ] **Step 4: Write failing runner and processor orchestration tests**

Update the requested-runner fixture so `_run()` supplies
`model_profile=self.gateway.profile`, then add:

```python
def test_new_operation_uses_supplied_request_profile(self):
    supplied = self.gateway.profile
    result = self.runner.run(
        self.source,
        product_name="ZDecision",
        template_id="business",
        model_profile=supplied,
    )
    operation = self.operation_store.operation_for_source(
        self.source.request_id, self.source.source_key
    )
    self.assertEqual(supplied.profile_id, operation.frozen.model_profile_id)
    self.assertEqual(0, self.gateway.discover_count)


def test_operation_profile_reads_frozen_replay_profile(self):
    self._run()
    self.assertEqual(
        self.gateway.profile,
        self.runner.operation_profile(self.source),
    )
```

Extend `FakeCaptureRunner` in `tests/test_capture_request_processor.py` with
`resolve_calls`, `run_profiles`, `operation_profiles`, and the three new public
methods:

```python
def operation_profile(self, source):
    return self.operation_profiles.get(source.source_key)

def resolve_request_profile(self, profile):
    self.resolve_calls.append(profile)
    if self.resolve_error is not None:
        raise self.resolve_error
    return self.profile if profile is None else profile

def run(
    self,
    source,
    *,
    product_name,
    template_id,
    model_profile,
    heartbeat=None,
):
    self.call_count += 1
    self.run_profiles.append(model_profile)
    if self.error is not None:
        raise self.error
    if self.after_freeze is not None:
        callback = self.after_freeze
        self.after_freeze = None
        callback()
    return SessionCaptureResult(
        status="completed",
        source_key=source.source_key,
        capture_operation_id="cap_" + "5" * 32,
        inventory_turn_id="inventory-turn",
        extraction_turn_id="extraction-turn",
        observations=(observation(source.upper_turn_id),),
        evidence_digest="b" * 64,
        model_profile=model_profile,
    )
```

Add processor tests with these exact assertions:

- no changed source causes zero profile-resolution calls;
- staged delivery and committed reconciliation replay cause zero profile
  resolution calls;
- two changed sources receive one identical request profile;
- a pre-amendment frozen request with no operation resolves and stores once;
- one pre-existing operation supplies the request profile for remaining new
  operations;
- mixed pre-existing operation profiles fail terminally before model work;
- an unavailable frozen profile maps exactly to
  `frozen_model_unavailable`, not `unexpected_processor_error`.
- corrupt private request-profile JSON maps to
  `local_capture_state_invalid`, while a conflicting valid profile maps to
  `model_profile_mismatch`.

- [ ] **Step 5: Run runner and processor tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_requested_capture \
  tests.test_capture_request_processor \
  -v
```

Expected: FAIL on the missing `model_profile` parameter and request-profile
orchestration methods.

- [ ] **Step 6: Make the runner consume, never discover, the request profile**

In `RequestedCaptureRunner`:

```python
def operation_profile(self, source):
    operation = self.operation_store.operation_for_source(
        source.request_id, source.source_key
    )
    return None if operation is None else _profile(operation.frozen)

def resolve_request_profile(self, profile):
    try:
        if profile is None:
            return self.gateway.resolve_active_profile()
        return self.gateway.require_supported_profile(profile)
    except FrozenModelProfileUnavailable as error:
        raise FrozenModelUnavailable("Frozen Capture model is unavailable") from error
    except (AppServerError, AppServerGatewayError) as error:
        raise CaptureAttemptRetryable("Capture model resolution must be retried") from error
```

Change `run` to require `model_profile`, remove
`discover_and_freeze_profile(boundary)`, create new operations with the supplied
profile, and extend `_verify_replay_input` to require equality with every frozen
profile field.

- [ ] **Step 7: Resolve one profile in the processor before source operations**

Keep staged delivery and committed reconciliation replay ahead of all model
work. Only when no reconciliation result exists, use this exact profile path
before `_capture_sources`:

```python
sources = self.session_index.freeze_sources(...)
self._require_matching_local_mapping(request)
staged = self.request_state.staged_batch(request.request_id)
if staged is not None:
    self._deliver(request, client, staged)
    return
result = self.request_state.get_reconciliation(request.request_id)
if result is None and sources:
    frozen = self.session_index.request_model_profile(request.request_id)
    operation_profiles = tuple(
        profile
        for source in sources
        if (profile := self.capture_runner.operation_profile(source)) is not None
    )
    distinct = {profile.profile_id: profile for profile in operation_profiles}
    if len(distinct) > 1:
        raise TerminalCaptureRequestError("model_profile_mismatch")
    operation_profile = next(iter(distinct.values()), None)
    if frozen is not None and operation_profile not in (None, frozen):
        raise TerminalCaptureRequestError("model_profile_mismatch")
    candidate = frozen if frozen is not None else operation_profile
    request_profile = self.capture_runner.resolve_request_profile(candidate)
    request_profile = self.session_index.freeze_request_model_profile(
        request.request_id, request_profile
    )
else:
    request_profile = None
```

When `result is None`, pass `request_profile` to every source run and assert it
is non-null at that boundary. This performs exactly one Gateway
resolution/validation call for an uncaptured non-empty processor attempt and
none for an empty request, staged delivery replay, or committed reconciliation
replay.

Catch `FrozenModelUnavailable` before the general requested-capture failures
and map it to `TerminalCaptureRequestError("frozen_model_unavailable")`.
Profile discovery/transport failures remain explicit retryable Capture errors.
Map `RequestModelProfileConflict` to terminal `model_profile_mismatch` and
`RequestModelProfileCorrupt` to terminal `local_capture_state_invalid`.

- [ ] **Step 8: Run Task 2 tests and verify GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_session_index \
  tests.test_requested_capture \
  tests.test_capture_request_processor \
  -v
```

Expected: all tests PASS, including existing zero-source and crash-replay tests.

- [ ] **Step 9: Commit Task 2**

```bash
git add src/zdecision/agent/session_index.py \
  src/zdecision/app_server/requested_capture.py \
  src/zdecision/agent/capture_processor.py \
  tests/test_session_index.py \
  tests/test_requested_capture.py \
  tests/test_capture_request_processor.py
git commit -m "fix: freeze model profile per capture request"
```

---

### Task 3: Prove Gate C and Resume the Real Request

**Files:**
- Modify: `tests/integration/test_on_demand_capture_core.py`
- Verify only: local technical-demo state under
  `/Users/zhaohuiying/Library/Application Support/ZDecision`
- Verify only: `/tmp/zdecision-inline-acceptance.DCOaOq/central.sqlite3`

**Interfaces:**
- Consumes all Task 1 and Task 2 APIs.
- Produces no new production API.

- [ ] **Step 1: Add the Gate C integration regression**

Extend `FakeAppServerGateway` with a current catalog and the new resolution
methods. Add one integration test that:

1. observes Session A and Session B;
2. resolves one active profile and begins a multi-source request;
3. simulates a catalog digest change that still supports the active pair;
4. restarts the local Agent between native attempts;
5. completes the request;
6. asserts both operations and reconciliation use the same profile ID;
7. asserts Candidate effects and handled checkpoints occur exactly once.

Add a second assertion path in the same test fixture showing an old request
freeze with `model_profile_json IS NULL` is populated on retry before its first
operation.

- [ ] **Step 2: Run the integration regression and verify RED, then GREEN**

Run before adapting the fixture:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_on_demand_capture_core.OnDemandCaptureCoreTest.test_catalog_change_keeps_one_request_profile_across_restart \
  -v
```

Expected RED: the fake Gateway and processor do not yet expose the new profile
contract.

After the minimal fixture and assertion changes, rerun the same command.
Expected GREEN: PASS with one profile ID and one Candidate effect.

- [ ] **Step 3: Run the bounded focused suite**

```bash
.venv/bin/python -m unittest \
  tests.test_app_server_gateway \
  tests.test_session_index \
  tests.test_requested_capture \
  tests.test_capture_request_processor \
  tests.integration.test_on_demand_capture_core \
  tests.integration.test_inline_candidate_refresh \
  -v
```

Expected: PASS. If any test fails, do not restart the live central service.

- [ ] **Step 4: Run the full repository suite once**

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: `OK`, with only existing environment-gated skips. Do not start a new
wide review after this run.

- [ ] **Step 5: Commit the integration regression**

```bash
git add tests/integration/test_on_demand_capture_core.py
git commit -m "test: cover runtime model profile rotation"
```

- [ ] **Step 6: Preflight against the real current app-server while central is stopped**

Restart the installed LaunchAgent so it loads the editable source, but keep the
central service stopped. Then run one read-only Gateway probe against the live
Agent database:

```bash
launchctl kickstart -k gui/$(id -u)/com.zdecision.agent
.venv/bin/python - <<'PY'
from pathlib import Path
from zdecision.agent.db import AgentDatabase
from zdecision.app_server.gateway import AppServerGateway

path = Path.home() / "Library/Application Support/ZDecision/agent/zdecision.sqlite3"
database = AgentDatabase.open(path)
gateway = None
try:
    gateway = AppServerGateway.connect(database=database)
    profile = gateway.resolve_active_profile()
    print(profile.profile_id, profile.model_id, profile.reasoning_effort)
finally:
    if gateway is not None:
        gateway.close()
    database.close()
PY
```

Expected: one valid `fmp_...` line and no conflict. Verify the installed Agent
is running before consuming the final central retry:

```bash
launchctl print gui/$(id -u)/com.zdecision.agent | rg 'state = running|pid ='
```

- [ ] **Step 7: Resume exactly the existing request and wait for a terminal state**

Start the central service with the preserved files:

```bash
.venv/bin/zdecision-central run \
  --database /tmp/zdecision-inline-acceptance.DCOaOq/central.sqlite3 \
  --config /tmp/zdecision-inline-acceptance.DCOaOq/central.json \
  --host 127.0.0.1 \
  --port 8765
```

Poll the preserved request by database state; do not create or click another
request:

```sql
SELECT request_id, state, attempt_count, result_candidate_count, terminal_code
FROM capture_requests
WHERE request_id = 'crq_407361107583b1b276f27e709fd41762';
```

Expected: `succeeded` or `succeeded_no_candidates` on attempt 5. If it becomes
`failed_terminal`, stop and report the exact event code; do not manufacture a
replacement request.

- [ ] **Step 8: Verify final invariants and stop**

Verify:

- request events contain `capturing_sessions`, optional
  `reconciling_candidates`, `uploading_candidates`, and one terminal success;
- `capture_request_freezes.model_profile_json` is non-null;
- every local `capture_operations.frozen_json` for the request contains the
  same profile ID as the request freeze;
- the handled checkpoint advances only after the central receipt;
- central `candidate_revisions` contains only structured Candidate records and
  no Prompt, transcript, source code, or tool output;
- `git status --short` is clean.

Record the Candidate count and request terminal state. Do not run another
review cycle or unrelated hardening task.

---

## Stop Rule

This plan ends after three commits, one focused suite, one full suite, one
read-only live preflight, and one attempt to resume the preserved request. Any
remaining inline auto-render reliability issue is a separate task and must not
be folded into this implementation.
