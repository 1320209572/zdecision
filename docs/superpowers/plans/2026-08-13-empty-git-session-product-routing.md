# Empty-Git Session Product Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When frozen Git path evidence is empty, route Candidate refresh by asking the configured Capture model to select one registered leaf route from each frozen Session boundary.

**Architecture:** Keep deterministic Git routing unchanged. Add one read-only Session routing component over the existing app-server gateway, freeze its structured selections inside the existing immutable `CaptureGroupPlan`, and filter each leaf slice to the sources assigned to it. The fallback has no UI, Central, MR, or user-product-input changes.

**Tech Stack:** Python 3.12, SQLite, Codex app-server structured turns, `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-13-empty-git-session-product-routing-design.md`

## Global Constraints

- The fallback runs only when `FrozenGitPathEvidence.paths == ()`.
- Any non-empty Git evidence uses the existing path matcher and never invokes Session model routing.
- The model may select only one enabled `route_id` from the frozen server route snapshot per source.
- No raw Session text, prompt, source code, or model explanation enters the routing database or Central.
- A persisted plan is immutable and suppresses another routing model call after restart.
- No confidence threshold, user product prompt, MR integration, or new Central schema is introduced.

---

### Task 1: Freeze semantic selections in the existing Capture plan

**Files:**
- Modify: `src/zdecision/agent/capture_routing.py`
- Modify: `tests/test_capture_routing.py`

**Interfaces:**
- Produces: `SessionRouteDecision(source_key: str, route_id: str, output_digest: str)`.
- Produces: `plan_session_routed_capture_group(group, snapshot, evidence, sources, decisions) -> CaptureGroupPlan`.
- Produces: `CaptureRoutingStore.get_or_create_session_plan(...) -> CaptureGroupPlan`.
- Changes: `CaptureGroupPlan` records `routing_method` and `session_route_decisions`; legacy stored path plans still load as `git_paths`.

- [ ] **Step 1: Write failing routing-plan tests**

  Add tests whose hand-derived expectations prove that empty evidence plus two source decisions creates two route slices with disjoint `source_keys`, empty `matched_paths`, and `routing_method == "session_model"`; invalid source coverage, disabled/out-of-snapshot routes, and non-empty evidence are rejected.

- [ ] **Step 2: Run the focused test and verify RED**

  Run: `.venv/bin/python -m unittest tests.test_capture_routing -v`

  Expected: import or attribute failures for `SessionRouteDecision` and `plan_session_routed_capture_group`.

- [ ] **Step 3: Implement the minimal immutable plan extension**

  Add the strict decision value and semantic planner. Use the same source-boundary digest and empty-path digest as existing plans. Persist the full new plan with the existing canonical JSON and digest checks. Accept the old six-field plan JSON only as a legacy `git_paths` record so installed state remains readable.

- [ ] **Step 4: Prove persistence and replay**

  Add a test that writes a semantic plan, reopens the store, supplies conflicting decisions, and receives the byte-equivalent first plan. Assert the stored JSON contains only source keys, route IDs, and output digests—not Session text.

- [ ] **Step 5: Run focused tests GREEN**

  Run: `.venv/bin/python -m unittest tests.test_capture_routing -v`

  Expected: all tests pass.

### Task 2: Classify one frozen Session through a structured app-server turn

**Files:**
- Create: `src/zdecision/app_server/session_product_routing.py`
- Create: `tests/test_session_product_routing.py`

**Interfaces:**
- Produces: `SessionProductRouter(gateway, recall_host_store)`.
- Produces: `route(source, snapshot, profile, heartbeat=None) -> SessionRouteDecision`.
- Produces errors: `SessionProductRoutingRetryable` for failed model transport; `SessionProductRoutingInvalid` for malformed/out-of-snapshot output.

- [ ] **Step 1: Write the missing-router RED**

  Build a complete fake gateway with an interactive completed source, a fork that inherits a Session mentioning `third-party-services`, and a structured result selecting the literal registered route. Assert the router returns the source key, selected route ID, and receipt output digest.

- [ ] **Step 2: Run the focused test and verify RED**

  Run: `.venv/bin/python -m unittest tests.test_session_product_routing -v`

  Expected: `ModuleNotFoundError` for `zdecision.app_server.session_product_routing`.

- [ ] **Step 3: Implement the minimal router**

  Verify the source is interactive, read the exact completed boundary, require matching CWD, fork exactly at `upper_turn_id`, bind the fork as an internal `capture` thread, and run one structured turn. The closed schema is:

  ```python
  {
      "type": "object",
      "properties": {"route_id": {"type": "string", "enum": route_ids}},
      "required": ["route_id"],
      "additionalProperties": False,
  }
  ```

  The prompt enumerates only enabled route IDs and path prefixes and directs the model to use inherited Session conversation without tools. Validate the receipt thread/profile and exact output before returning `SessionRouteDecision`.

- [ ] **Step 4: Add failure and privacy tests**

  Prove wrong CWD, missing boundary, non-interactive source, malformed output, disabled/unknown route, and receipt mismatch fail closed. Assert prompts do not contain Session/Turn IDs and structured outputs do not persist conversation text.

- [ ] **Step 5: Run focused tests GREEN**

  Run: `.venv/bin/python -m unittest tests.test_session_product_routing -v`

  Expected: all tests pass.

### Task 3: Invoke the fallback only for empty Git evidence

**Files:**
- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `src/zdecision/agent/service.py`
- Modify: `tests/test_capture_request_processor.py`
- Modify: `tests/test_agent_service.py`
- Modify: `tests/integration/test_on_demand_capture_core.py`

**Interfaces:**
- `OnDemandCaptureProcessor.__init__` consumes `session_router`.
- Existing `capture_runner.resolve_request_profile(profile)` remains the model-profile seam; the selected profile is frozen in `SessionIndex` before routing and reused by Extraction.

- [ ] **Step 1: Write processor REDs**

  Add behavior tests proving: non-empty Git evidence has zero router calls; empty evidence calls the router once per frozen source; selections are persisted before `client.plan_slices`; and each Capture slice receives only sources listed in its `CaptureSlicePlan.source_keys`.

- [ ] **Step 2: Run the processor tests and verify RED**

  Run: `.venv/bin/python -m unittest tests.test_capture_request_processor -v`

  Expected: constructor/behavior failures because `session_router` is not wired and empty evidence still terminalizes with no slices.

- [ ] **Step 3: Implement processor selection**

  Preserve the existing `load_plan` fast path. When no plan exists, freeze Git evidence once. Call the path planner for non-empty evidence. For empty evidence, resolve and freeze the request model profile, route each source, then call `get_or_create_session_plan`. Map routing transport errors to retryable Capture errors and invalid routing/boundary errors to bounded terminal Capture errors.

- [ ] **Step 4: Filter sources per slice**

  Before `_process_slice`, intersect frozen sources with `slice_plan.source_keys` and the existing exclusion set. Existing Git plans continue to list every frozen source, so their behavior is unchanged.

- [ ] **Step 5: Wire the production service**

  Construct one `SessionProductRouter` with the same validated `AppServerGateway` and `RecallHostStore` used by `RequestedCaptureRunner`. Extend the service test to assert the production processor owns this router and shares the gateway.

- [ ] **Step 6: Run processor and integration tests GREEN**

  Run:

  ```bash
  .venv/bin/python -m unittest \
    tests.test_capture_request_processor \
    tests.test_agent_service \
    tests.integration.test_on_demand_capture_core -v
  ```

  Expected: all tests pass, including a real fake-app-server vertical in which an empty Git boundary mentioning `third-party-services` produces that registered leaf slice and Candidate.

### Task 4: Amend product authority and Plugin instructions

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/superpowers/specs/2026-08-05-monorepo-product-routing-and-batch-review-design.md`
- Modify: `plugins/zdecision/skills/candidate-refresh/SKILL.md`
- Modify: `tests/test_skill_contract.py`

**Interfaces:**
- Produces no runtime API. Aligns authoritative and agent-facing behavior with Tasks 1–3.

- [ ] **Step 1: Add the agent-behavior RED**

  Update the Skill contract test to require all three observable rules: Git path evidence is preferred, an empty Git path set uses the frozen Session model fallback, and the user is never asked to choose a product.

- [ ] **Step 2: Run and verify RED**

  Run: `.venv/bin/python -m unittest tests.test_skill_contract -v`

  Expected: failure because the installed Skill still describes Git-only routing.

- [ ] **Step 3: Amend docs and Skill**

  Replace the absolute model-routing prohibition only in the zero-Git case. Preserve the prohibitions on Candidate paths, browser/upload fields, disabled/unregistered routes, broad Shared fallback, and non-empty unmatched Git evidence.

- [ ] **Step 4: Run all focused verification**

  Run:

  ```bash
  .venv/bin/python -m unittest \
    tests.test_capture_routing \
    tests.test_session_product_routing \
    tests.test_capture_request_processor \
    tests.test_agent_service \
    tests.integration.test_on_demand_capture_core \
    tests.test_skill_contract -v
  .venv/bin/python -m compileall -q src/zdecision tests
  git diff --check
  ```

  Expected: all tests and static checks pass.

- [ ] **Step 5: Review and commit**

  Confirm only the declared feature files are staged, preserve all pre-existing untracked files, and commit with `feat: route empty Git captures from Session context`.
