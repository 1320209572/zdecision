# Strict Business Decision Template Implementation Plan

> **For Codex:** Execute this plan inline with test-driven development. The user has already approved the design and requested implementation.

**Goal:** Raise the precision of the default business decision compression template so it retains durable product decisions while rejecting rediscoverable technical facts, ordinary correctness, and fragmented restatements of one principle.

**Architecture:** Keep the two-stage Capture protocol and renderer-owned envelopes unchanged. Revise only the editable Stage 2 policy for template `business`, publish it as revision 2, and update current documentation. A future technical-contract template may retain API-level facts; it is not part of this change.

**Tech Stack:** Markdown policy templates, JSON manifest, Python `unittest`.

## Scope and stopping rule

- Change `decision-templates/business/extract.md` and bump `decision-templates/business/manifest.json` from revision 1 to 2.
- Add contract tests in `tests/test_templates.py` before changing the policy.
- Update only current architecture/usage documentation that identifies the bundled revision or its semantics. Historical revision-1 design and implementation plans remain historical records.
- Do not change the Capture protocol, schemas, candidate limit, Stage 1 inventory policy, CLI, persistence, or native Codex transport.
- Stop after focused tests, one full test run, one real 安恒 Capture comparison, and a concise result report. Do not start another broad review loop.

## Task 1: Specify revision 2 in tests

**Files:**
- Modify: `tests/test_templates.py`

1. Change the bundled template identity expectations from revision 1 to revision 2.
2. Add a policy contract test requiring all of these semantics in the rendered extraction prompt:
   - a retained item must represent a deliberate, non-obvious, durable product choice;
   - rediscoverable endpoint, method, header, field, format, enum, default, and range facts are excluded unless they encode an explicit business or compatibility choice;
   - ordinary implementation correctness and “Bug fixed” confirmation are insufficient;
   - signals expressing the same product principle yield only the most complete representative, without merging their contents or provenance;
   - technical contracts belong in a separate template category.
3. Run the focused test and confirm it fails for the current revision-1 policy.

## Task 2: Implement the strict policy

**Files:**
- Modify: `decision-templates/business/extract.md`
- Modify: `decision-templates/business/manifest.json`

1. Rewrite the Stage 2 business policy as a high-precision decision-worthiness gate.
2. Preserve product capability boundaries, ownership placement, business flows, permissions, compatibility policy, and user-visible semantics.
3. Add the explicit exclusions and representative deduplication rule covered by the tests.
4. Bump the manifest revision to 2 and rerun the focused tests.

## Task 3: Align current documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`

1. Identify the bundled default as `business` revision 2.
2. Explain the business-versus-technical-contract boundary and representative deduplication behavior.
3. Leave dated revision-1 design and implementation plans unchanged.

## Task 4: Verify and perform one real acceptance

1. Run the focused template tests.
2. Run the complete test suite once and run `git diff --check`.
3. Commit the scoped implementation.
4. Run one two-stage Capture against task `019f5f21-0d48-7501-9dd5-0219870232a1` with product `安恒` and template `business` revision 2.
5. Compare the new Candidate count and themes with the prior 21-item revision-1 result. Report exclusions and any remaining quality issues without launching another repair or review cycle.
