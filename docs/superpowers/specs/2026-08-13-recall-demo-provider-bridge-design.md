# Recall Demo Provider Bridge Design

**Status:** Approved in chat on 2026-08-13; written specification pending user review

## Purpose

Build one leadership-demo vertical that connects the existing ZDecision
Candidate and Central publication workflow to the already validated local
Recall Demo. The resulting demonstration must show one reviewed Decision
moving through this exact lifecycle:

1. the user asks ZDecision to update Candidate decisions;
2. the inline Candidate card opens the Decision Center in the default browser;
3. the user reviews and publishes the Candidate;
4. the completed publication refreshes the local signed Demo Recall bundle;
5. a new Codex task selects ZDecision and confirms Recall;
6. the local Demo retriever returns the newly published formal Decision;
7. the existing Gate A handoff classifies and atomically applies it before any
   affected code change.

This is an explicitly bounded Demo. It is not Gate B, Gate C, Gate D, or a
production-readiness claim.

## Existing Capabilities Reused Without Redesign

The following production paths already exist and remain authoritative:

- the Candidate refresh card and its current/all-session controls;
- the trusted local Candidate capture binding;
- the default-browser `open_zdecision_dashboard` action;
- Central review, preview, publish, exact Registry commit, and push;
- the formal `DecisionRevision` representation in `decision-registry/`;
- the Gate A Recall confirmation card;
- next-native-message context delivery;
- four-way applicability classification;
- atomic application and mutation gating.

The standalone prototype at `prototypes/recall_demo/` has already validated:

- an Ed25519-signed Decision bundle;
- two pinned local model revisions and exact model-file bindings;
- offline model loading;
- BM25, dense, exact-path, weighted fusion, reranking, thresholding, and
  bounded complete-Decision packing;
- deterministic, fail-closed retrieval contracts.

The bridge promotes and adapts those parts. It does not invent a second card,
a second publication path, or a second application state machine.

## Demo Boundary

The Demo supports only:

- repository `zstack-ui-next`;
- product `third-party-services`;
- Decision Space `prod_3e6e73b8defbfee89ce7bf26e739b1dc`;
- the two model revisions already pinned by the prototype profile;
- one local operator and one local Decision Center deployment;
- a bounded active corpus of 1 through 32 formal Decisions;
- local filesystem state owned by the current OS user.

The Demo does not implement:

- multi-product Recall;
- Central background distribution or polling;
- last-known-good rollback policy across machines;
- production key management or rotation;
- a representative retrieval benchmark or quality percentage;
- automatic model download during Recall;
- persisted/shared vector indexes;
- production observability, fleet rollout, or disaster recovery.

## Architecture

### 1. Promote the validated retrieval core

Move the reusable prototype implementation into an installable package under
`src/zdecision/recall/demo/`. The package owns bundle verification, the frozen
profile, model-state verification, projection, in-memory indexing, runtime
loading, and hybrid retrieval.

The standalone CLI may remain as a thin operator surface, but it must import
the installable package rather than retain a divergent copy. Prototype tests
move with the promoted implementation and retain their security and ranking
assertions.

The fixed-corpus assumptions are changed only where necessary for the demo
publication loop:

- replace “exactly ten Decisions” with 1 through 32 active heads;
- accept every positive formal head revision rather than only revision 1;
- continue requiring one unique active head per Decision identity;
- continue signing the exact ordered leaf identities, revisions, profile,
  snapshot bytes, and bundle file bindings;
- keep the existing shortlist limits of 8 items and 10,000 UTF-8 bytes.

### 2. Immutable Demo bundle generations

Demo Recall state lives outside Git in the existing private ZDecision state
root. A small owner-only configuration record identifies:

- the external Demo signing key;
- the public trust root;
- the signed retrieval profile;
- the prepared model-state root;
- the `third-party-services` Registry product root;
- the immutable bundle-generation directory.

Each successful refresh writes a new immutable generation named by the exact
completed publication commit. Publication never overwrites a bundle directory.
After building and verifying the new bundle, one atomic `current.json` replace
selects it. Failed build, signature, or pointer validation leaves the previous
generation selected.

The private key remains outside the repository and outside every bundle. The
Agent needs only the public trust root to read a selected bundle. The Decision
Center's local publication process is the only process that needs the private
key for automatic Demo refresh.

### 3. Refresh after Central publication

`CentralApplication._synchronize_completed_publication()` is the existing
post-publication boundary. After the publication is `completed` and the normal
Registry projection succeeds, an optional Demo publisher refreshes the signed
bundle for `third-party-services`.

Rules:

- no Candidate enters the bundle before publication is completed;
- only formal active Registry heads enter the bundle;
- the refresh is idempotent for the publication commit;
- an existing matching generation is verified and reused;
- a mismatching existing generation fails closed;
- a refresh failure never corrupts or replaces the previous selected bundle;
- when Demo mode is configured, a failed refresh returns a bounded
  `recall_demo_refresh_failed` result so the presenter does not falsely claim
  that the new Decision is recallable;
- retrying/resuming the already completed publication retries only the Demo
  refresh and does not publish a second Registry commit.

Outside configured Demo mode, Central behavior is byte-for-byte compatible and
does not attempt a Recall bundle refresh.

### 4. `DemoRecallProvider`

Add one provider implementing the existing production seam:

```python
class DemoRecallProvider:
    def preflight(
        self,
        *,
        repository_id: str,
        repository_display_name: str,
        intent: RecallIntent,
        now: datetime,
    ) -> RecallPreflightResult: ...

    def retrieve(self, preflight: RecallPreflightReady) -> RecallShortlist: ...
```

`preflight()` is Hook-safe and bounded. It must not import Torch, load models,
build an index, perform network I/O, or read Decision prose into Hook output.
It validates the configured Demo identity, selected bundle pointer, signed
bundle metadata, prepared-model activation metadata, repository, Decision
Space, and expiry. It returns:

- `RecallPreflightReady` only for the exact configured product and generation;
- `RecallPreflightClarification` when the intent does not uniquely select
  `third-party-services`;
- `RecallPreflightUnavailable(code="recall_not_ready")` for missing, stale,
  invalid, or unprepared Demo state.

`retrieve()` revalidates that the bundle and model generation still match the
frozen preflight. It then loads the prepared local models, builds the bounded
in-memory index, runs the existing hybrid retriever, and converts each result
to `RecalledDecision`. The returned `RecallShortlist` contains complete formal
Decision revisions, canonical digests, and deterministic match reasons.

The MCP process may cache the verified runtime and in-memory index by exact
bundle/profile/model-generation digest. A generation change creates a new
cache entry; it never mutates an entry in place. Hook processes do not use this
cache and perform only preflight.

### 5. Wire the same Provider into Hooks and MCP

The current Hook CLI and MCP startup both default to
`UnavailableRecallProvider`. Demo mode must construct the same provider from
the same owner-only configuration in both processes:

- the Hook path uses `preflight()` to create the trusted activation attempt;
- the MCP path uses `retrieve()` only after the user clicks the confirmation
  card;
- the preflight's bundle, profile, model, and generation digests bind both
  processes to the same selected data;
- if either process observes another generation or invalid state, the current
  attempt fails closed and a new task/confirmation is required.

When Demo mode is absent or invalid, both paths continue using
`UnavailableRecallProvider`. No environment variable, model-authored field,
tool argument, or card payload may select a private path or override the
configured identity.

## Data Flow

```text
User: 更新候选决策
  -> existing Candidate card
  -> existing Capture / Candidate upload
  -> existing Decision Center review
  -> existing publish commit and push
  -> Registry projection
  -> Demo bundle generation + signature verification
  -> atomic current bundle pointer

New Codex task + ZDecision selection
  -> DemoRecallProvider.preflight()
  -> existing Recall confirmation card
  -> user enables Recall
  -> DemoRecallProvider.retrieve()
  -> existing frozen Gate A handoff
  -> next native message
  -> existing classification + atomic application
  -> report result; do not modify code during the demo acceptance
```

## Failure Behavior

- An unpublished Candidate is never present in the signed bundle.
- A completed publication whose Demo refresh failed is reported as not ready;
  the previous bundle remains valid but must not be presented as containing the
  new Decision.
- Missing private signing configuration affects only post-publication Demo
  refresh, never Registry publication integrity.
- Missing trust root, invalid signature, altered snapshot, changed model
  pointer, unavailable local models, wrong repository, or wrong Decision Space
  returns `recall_not_ready` without leaking paths or key material.
- Retrieval runtime failure returns the existing bounded delivery failure and
  does not fall back to keyword-only or Central/network retrieval.
- Empty retrieval is a valid complete shortlist and does not enable mutation
  based on an invented Decision.
- The provider performs no network access. Model download remains an explicit
  setup action before the leadership demo.

## Configuration and Setup

Add one explicit setup command that validates and atomically writes the
owner-only Demo configuration. It accepts absolute external paths for the
private key, trust root, profile, prepared model state, Registry product root,
and bundle state root. It prints only bounded readiness and digest prefixes;
it never prints keys, Decision content, or absolute paths.

Setup must be completed before the Decision Center and Codex Desktop are
started for the demo. The Plugin does not ask the model or user to supply
filesystem paths during a task.

## Verification

### Automated

Tests must prove:

1. 10 active revision-1 Decisions still build and retrieve as before.
2. A newly published 11th Decision is included in the next generation.
3. A revision-2 active head replaces revision 1 without duplicate identity.
4. Unpublished Candidate bytes never enter a bundle.
5. Failed refresh preserves the previous `current.json` generation.
6. Hook preflight performs no model load, network call, or retrieval.
7. MCP retrieval maps prototype results to exact `RecallShortlist` values.
8. Hook and MCP reject cross-generation, cross-product, cross-repository, and
   altered-bundle reuse.
9. Demo mode absent retains the current unavailable-provider behavior.
10. Candidate, Central publication, Capture isolation, Recall card, handoff,
    application, and mutation-guard focused suites remain green.

### Leadership-demo acceptance

Use one Decision with a conspicuous title or literal that is absent from the
initial signed bundle.

1. Before publication, run a related Recall task and show that this Decision
   is not returned.
2. Ask ZDecision to update Candidate decisions.
3. Click **打开决策中心** and show the Candidate in the default browser.
4. Review and publish it.
5. Confirm the Decision Center reports publication and Demo refresh success.
6. Start a new Codex task and select ZDecision.
7. Describe the matching `third-party-services/security-services` goal.
8. Show the Recall confirmation card and click enable.
9. Send the next native message with the attached handoff.
10. Show that the conspicuous published Decision is classified, applied, and
    reported with `application_committed`.
11. Stop without modifying product code.

Acceptance fails if the Decision is recallable before publication, missing
after successful refresh, obtained from Central/network during Recall, or if
the flow requires guessing any task/session/attempt/delivery identifier.

## Estimated Implementation Scope

This design is one Demo slice, expected to fit in roughly one working day plus
a bounded Desktop rehearsal. Most code already exists; the substantive work is
promotion of the prototype package, dynamic active-head support, the automatic
publication bridge, the `DemoRecallProvider` adapter, and shared Hook/MCP
wiring.
