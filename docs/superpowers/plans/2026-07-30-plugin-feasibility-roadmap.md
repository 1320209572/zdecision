# ZDecision Plugin Feasibility Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement each detailed plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the approved pre-Demo Plugin loop in dependency order without
implementing later subsystems before the early feasibility gates establish that
the architecture can run inside Codex.

**Architecture:** The work is split into five execution packets. Each packet
ends in a user-visible Gate and produces the stable interfaces consumed by the
next packet. A failed blocking Gate stops the roadmap at that boundary; it does
not cause later packets to be implemented speculatively.

**Tech Stack:** Python 3.11+, SQLite, canonical JSON, Codex Plugin Skills and
Hooks, MCP Python SDK 1.x, Codex app-server JSONL protocol, the existing V1
Capture and Registry modules, a minimal central HTTP/MCP service, and
`unittest`.

## Global Constraints

- The authority is
  `docs/superpowers/specs/2026-07-30-plugin-feasibility-design.md`, with
  `docs/architecture.md` remaining authoritative for existing manual V1
  behavior.
- Work directly on `main`; do not create a worktree, feature branch, Registry
  branch, or parallel implementation tree.
- Raw Sessions, Prompts, model context, source diffs, source code, and complete
  Thread data never leave the device.
- Hooks must remain bounded and non-blocking; they do not run models, Capture,
  or synchronous network requests.
- The existing two-stage Capture and V1 Registry formats are reused. Do not
  introduce a Decision V2, automatic Decision mutation, production SSO, or
  production visual design.
- Each Gate receives one recommended implementation, only its predeclared
  fallback, and at most one focused correction of a confirmed defect.
- After a Gate passes, commit it before starting the next Gate. Do not run
  repeated broad reviews or blind audit loops.
- A packet is not allowed to weaken a preceding Gate in order to pass a later
  one.

---

## Dependency order

```text
Packet A: Gates 1-3
  Plugin package -> local Agent runtime -> app-server Capture proof
            |
            v
Packet B: Gates 4-5
  Capture eligibility -> Review readiness -> Candidate convergence
            |
            v
Packet C: Gates 6-7
  Central identity/data contracts -> Web Review -> V1 publication
            |
            v
Packet D: Gate 8
  Signed cold-start cache -> sync -> local ranking and injection
            |
            v
Packet E: Gate 9
  zero-touch completion -> multi-Session real end-to-end acceptance
```

Later detailed plans are written only after the preceding blocking packet
passes. This is an explicit stopping rule, not an unfinished placeholder.

## Packet A: Plugin runtime and app-server proof

**Detailed plan:**
`docs/superpowers/plans/2026-07-30-plugin-runtime-app-server.md`

**Covers:** Gates 1, 2, and 3.

**Produces:**

- an installable local Plugin package with one Skill, five lifecycle Hooks, and
  a bundled local MCP server declaration;
- an SQLite Event Ledger and on-demand singleton Worker with active-session
  leases;
- a typed JSONL app-server client and frozen `FeasibilityModelProfile`;
- an automated runner that uses a separate eligibility fork followed by one
  fresh two-Turn Capture fork; and
- live Gate evidence from a real Hook Session without transcript parsing.

**Blocking result:** If neither a supported host connection nor the controlled
`codex app-server --listen stdio://` process can read and fork the Hook Session,
stop the roadmap. Do not start Packet B.

## Packet B: Automatic Capture and Candidate convergence

**Plan creation trigger:** Packet A Gate 3 passes with its exact model profile
and native source/fork/Turn receipts.

**Covers:** Gates 4 and 5.

**Will produce:**

- fixed `capture-eligibility/v1` positive and negative fixture coverage;
- Work Unit assembly and strong-trigger scheduling with the 60-second settling
  timer used only after a strong trigger;
- exact Review-readiness evaluation separated from Capture eligibility;
- `candidate-reconciliation/v1`, per-product observation sequencing, stable
  Candidate families, revision lifecycle, invalidation controls, and replay;
  and
- real single- and cross-Session convergence evidence.

**Blocking result:** If ordinary completed work cannot be distinguished from
exploring, blocked, failed, or waiting work under the frozen Gate 3 model
profile, stop before any central service work.

## Packet C: Central Review and V1 publication

**Plan creation trigger:** Packet B Gates 4 and 5 pass.

**Covers:** Gates 6 and 7.

**Will produce:**

- a central SQL domain store with test identity and server-derived
  organization, actor, repository, and product;
- strict Candidate synchronization and invalidation allowlists;
- signed repository mapping and complete Decision snapshot endpoints;
- a functional product-isolated Review page and batch Web action transaction;
- a central Publication Record and Git Worker that reuse V1
  `RegistryCatalog` and `GitRegistryAdapter` behavior; and
- authorization, idempotency, stale Review, canonical V1 round-trip, and crash
  recovery evidence.

**Boundary:** The feasibility page is intentionally plain and uses test
identity. Company OIDC/SSO and production visual design remain outside this
packet.

## Packet D: Cold-start Recall and injection

**Plan creation trigger:** Packet C can publish one real V1 Decision and return
the new `decision_version` and `sync_cursor`.

**Covers:** Gate 8.

**Will produce:**

- signed, atomic local cache generations and onboarding from an empty data
  directory;
- incremental cursor synchronization during active-session Worker leases;
- the exact fresh/stale/expired/invalid offline policy;
- deterministic English and Chinese local ranking with the eight-Decision and
  10,000-byte cumulative context budget;
- Task Usage, route and context epochs, `active_injected_set`, and Prompt and
  Context Injection Receipts; and
- `UserPromptSubmit` and `SessionStart(compact|clear)` Hook output with no
  network call on the Prompt path.

**Blocking result:** Seeded caches and manual synchronization do not count. If
the empty-directory cold-start scenario fails, do not proceed to Gate 9.

## Packet E: Real acceptance

**Plan creation trigger:** Gates 1 through 8 pass once each.

**Covers:** Gate 9 and final stopping rules.

**Will produce:**

- the zero-touch single-Session completion scenario in which the user never
  mentions ZDecision and a Candidate reaches the Review Inbox automatically;
- the three-Session continuation/reversal, Web Review/publication, background
  sync, and first-Prompt Recall scenario;
- a central privacy audit proving forbidden raw content is absent; and
- one focused test run, one full repository test run, and one real acceptance
  report.

If Codex does not reliably call
`report_work_state(milestone_complete)` in the zero-touch scenario, Gate 9
fails. Only then may one evidence-derived deterministic readiness fallback be
specified; a manual submit cannot rescue the Gate.

## Stable packet interfaces

These boundaries may gain implementation detail in their owning packet, but a
later packet must not bypass them:

```python
@dataclass(frozen=True)
class FeasibilityModelProfile:
    model_id: str
    reasoning_effort: str


@dataclass(frozen=True)
class AppServerTurnReceipt:
    thread_id: str
    turn_id: str
    output_sha256: str
    model_profile_id: str


@dataclass(frozen=True)
class CandidateRevisionKey:
    candidate_id: str
    revision: int
    content_digest: str


@dataclass(frozen=True)
class DecisionWatermark:
    product_id: str
    decision_version: int
    sync_cursor: int
```

- Packet A owns Codex/app-server transport and local event persistence.
- Packet B owns eligibility, Candidate Observation/family semantics, and the
  transition to `review_ready`.
- Packet C owns authenticated shared Candidate state, Review, publication, and
  formal Decision truth.
- Packet D owns local signed-cache truth and model-visible Decision injection.
- Packet E composes public interfaces only; it may not add a second path around
  any owner.

## Roadmap completion rule

The feasibility roadmap is complete only when Gate 9 passes. Passing it does
not rename this work as the first Demo. The next artifact is a separate first
Demo specification covering company-email OIDC/SSO, distributable runtime
packaging, and production page design.
