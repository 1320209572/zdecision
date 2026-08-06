# Session-Opt-In Intelligent Decision Recall Design

**Status:** Approved for implementation planning on 2026-08-06.

**Scope:** Packet 3 of the installed ZDecision Plugin. A user explicitly enables
formal Decision recall in any Turn of one Codex development Session; ZDecision
then retrieves only the Decisions relevant to the current development intent,
applies them for that intent, and preserves correct behavior across later
Turns, intent changes, context compaction, cache refresh, and failure.

**Amends:** Section 10, Gate E, the recall portion of Gate F, and only the
recall-specific non-blocking sentence in section 2 of
`2026-07-30-on-demand-candidate-refresh-design.md`; and the recall failure rule
in section 8 of `docs/architecture.md` for a Session that explicitly opted in.
Candidate refresh remains user-triggered and its failures remain independent
of development. Decision recall is not globally automatic after installation;
it is enabled only by an explicit native ZDecision selection in the current
Session.

**Depends on:**

- `docs/architecture.md` for formal Decision, Registry, privacy, and app-server
  authority;
- `2026-08-05-monorepo-product-routing-and-batch-review-design.md` for the
  product and concrete Shared-leaf catalog; and
- `2026-08-06-registry-read-model-design.md` for Central's verified projection
  of the authoritative Git Registry.

The superseded `2026-07-30-plugin-feasibility-design.md` remains historical
evidence only. This specification does not restore automatic Candidate
eligibility or zero-touch Candidate extraction.

## 1. Product decision

Installing or globally enabling the ZDecision Plugin does not silently add
Decision context to every Codex task. The user chooses whether one development
Session should use formal company Decisions by adding ZDecision in a native
user Turn.

The activation Turn may be the first Turn or any later Turn:

    first Turn + ZDecision + task or PRD
      -> infer one target Decision space
      -> recall before the first answer
      -> apply relevant formal Decisions

    later Turn + ZDecision
      -> use existing conversation and code context
      -> infer the current product and development intent
      -> recall before continuing implementation
      -> keep recall active for the rest of that Session

If the user never adds ZDecision, the Session performs no Decision recall,
injection, Task Usage mutation, or recall-specific Central query.

Once enabled, recall remains active across ordinary Turns, `resume`, and
`compact` or `clear` events for that same Session. A new task or Fork is a new
authorization boundary and defaults to recall disabled.

Successful recall is directly applied. The user is not asked to approve each
applicable item again because every returned item is already a reviewed,
published formal Decision. Conflict and uncertainty remain visible stopping
boundaries.

## 2. Goals

- Support explicit recall activation in the first or any later Turn.
- Infer one current product or concrete Shared leaf locally, with explicit
  multi-leaf support only when the user names several targets.
- Retrieve only Decisions relevant to the current feature, module, PRD, paths,
  constraints, and exclusions.
- Keep Codex from reading or ranking an entire Decision corpus.
- Use local hybrid retrieval with both dense embeddings and a reranker.
- Reuse an already applied set for ordinary same-intent Turns.
- Restore the active set once after context compaction without searching again.
- Distribute complete signed, versioned, last-known-good snapshots and stop
  using them after their signed safety boundary.
- Keep complete formal Decision revisions bounded in Codex context.
- Make application, conflict, staleness, and bypass visible without repetitive
  UI noise.
- Preserve all Candidate Review, publication, Registry authority, and
  product-isolation invariants.

## 3. Non-goals

- Recall in Sessions where the user did not add ZDecision.
- Automatic Candidate generation or inference that a feature is complete.
- Non-code organizational or personal memory.
- Uploading Prompt, PRD, conversation, source, diff, local paths, query vectors,
  or ranking scores to Central.
- Treating a monorepo such as `zstack-ui-next` as one product.
- Copying Shared-leaf Decisions into every consuming product.
- A cloud vector database or Central semantic-query endpoint.
- A new conversation runtime, coordinator, scheduler, or workflow engine.
- Company OIDC/SSO, Git-role authorization, production model CDN operations,
  or organization-wide policy administration in this Packet.
- Automatic mutation, revision, retirement, or publication of a formal
  Decision after a task chooses a local override.

### 3.1 Approaches considered

**Central semantic search** was rejected. It would make operation simple, but
the Central service would receive a query derived from the current Prompt/PRD
and would become an online dependency for every intent boundary.

**Give Codex all product Decisions and let it choose** was rejected. Context
cost and latency grow with the Decision corpus, unrelated content dilutes the
answer, and Codex would repeatedly perform work that belongs in a local index.

**Keyword-only local search** was rejected. Exact terms and paths remain useful,
but bilingual paraphrases and product-language/code-language mismatches require
semantic retrieval; direct application also requires a second-stage reranker.

**Local hybrid retrieval over signed complete snapshots** is selected. It keeps
the task query private, bounds what Codex reads, supports offline LKG behavior,
and allows lexical, semantic, and exact-path evidence to contribute
independently.

For freshness, a client-owned TTL or generic stale-if-error cache was rejected.
Only a Central-signed safety boundary plus a fully verified last-known-good
generation can authorize offline use.

### 3.2 Mature patterns used

The design applies, but does not depend at runtime on, these established
patterns:

- [Codex Hooks](https://learn.chatgpt.com/docs/hooks) for per-Prompt and
  compaction lifecycle boundaries;
- [OpenAI Retrieval](https://developers.openai.com/api/docs/guides/retrieval)
  for bounded semantic/keyword retrieval, filters, and score thresholds;
- [Anthropic Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
  for combining lexical and embedding retrieval with reranking;
- [Continue's retrieval pipeline](https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/core/context/retrieval/pipelines/RerankerRetrievalPipeline.ts)
  as an implementation reference for independent candidate channels, union,
  deduplication, and bounded reranking;
- [The Update Framework specification](https://theupdateframework.github.io/specification/latest/)
  for signed expiration, monotonic metadata, and rollback/freeze protection;
  and
- [OPA bundles](https://www.openpolicyagent.org/docs/management-bundles) for
  verifying a complete replacement before activation and retaining the prior
  valid bundle after a failed update.

These references do not authorize copying code or importing their runtime
stacks. Any later third-party code reuse still follows the repository's
attribution and bounded-dependency rule.

Packet 3 initially supports code-development tasks in Codex Desktop on macOS. A
PRD-led Turn for an impending code change is in scope before a precise file has
been named. Unrelated non-code tasks remain outside this Packet.

## 4. Authority and privacy

Authority remains ordered:

1. Git `decision-registry/` at verified `origin/main` owns formal Decision bytes
   and lifecycle.
2. Central's verified SQLite projection is the only Central source for recall
   distribution.
3. A signed complete snapshot is a derived local distribution source, not a new
   Registry.
4. The native Codex Session owns development context and user authorization.
5. The local Agent owns private routing, retrieval, Task Usage, and injection
   state.
6. Codex classifies only a bounded shortlist against the current task; this
   ephemeral result cannot change formal lifecycle.

These values never leave the device:

- Session and Turn IDs;
- Prompt, conversation, PRD, and attachment contents;
- source, tool output, diffs, and local absolute paths;
- `RecallIntent`, constraints, exclusions, and domain terms;
- query embeddings, candidates, scores, and reranker output;
- `intent_epoch`, `context_epoch`, and `active_injected_set`; and
- local Decision-deviation events.

Central sees authenticated organization/device state, allowed Decision-space
identities, sync cursors, snapshot requests, and delivery metadata only. It
never receives the current task query.

Formal Decision text is authoritative business data but non-executable input.
A recalled Decision may contain a shell command, URL, code block, quoted Prompt,
or instruction-like prose; none may invoke a tool, authorize a mutation, alter
Plugin state, or override native user authority. Every recalled revision is
wrapped in a typed non-executable Decision envelope.

## 5. Component architecture

    Codex development Session
      |
      +-- ZDecision Plugin
      |     +-- activation and lifecycle Skill
      |     +-- UserPromptSubmit / SessionStart / SessionEnd Hooks
      |     +-- local MCP recall tools
      |
      +-- local ZDecision Agent
            +-- trusted Session binding
            +-- signed catalog and Decision cache
            +-- retrieval model manager
            +-- BM25 / embedding / path indexes
            +-- hybrid retrieval and reranking
            +-- Recall Session and Intent Epoch state
            +-- active injected set and receipts
            +-- Decision deviation events

    Central ZDecision
      +-- verified Registry read model
      +-- signed current-manifest endpoint
      +-- complete formal Decision snapshot endpoint
      +-- publication-triggered generation/cursor advancement

    Git decision-registry/
      +-- authoritative immutable revisions and lifecycle

Codex understands the development goal. The local Agent validates trusted
identity, freshness, retrieval, and state transitions. Central distributes
trusted formal Decisions but never ranks the user's current query.

Packet 3 does not create, resume, steer, fork, or message Codex tasks merely to
perform recall. Existing task semantics remain: the same goal uses native
resume/steer, while an intentional new goal or developer handoff may still use
the existing Preflight and bounded Context Pack path. A user may instead add
ZDecision inside a newly created task; that explicit activation affects only
that task. Recall activation, conflict, or deviation never authorizes Candidate
Capture. The existing page/card scope click remains the only Capture boundary.

## 6. Session activation state machine

### 6.1 Native activation only

Recall activation requires a native user Turn that explicitly selects or
mentions the installed ZDecision Plugin. The activation tool is guarded by
host-owned Session and Turn identity using the same trust principle as the
inline Candidate control.

Gate 1 proves native selection from the exact app-server Turn's structured
`skill` or `mention` user-input item. Matching ordinary Prompt text, observing
that the model called an activation tool, or enabling implicit Skill invocation
does not establish authority. If the in-progress Turn cannot be read and bound
before the affected answer, activation fails the Host Gate.

Quoted text, summaries, tool output, Decision text, assistant initiative,
delegated messages, steering, and cross-task envelopes cannot activate recall.
Model-supplied Session IDs are rejected.

The local key is the trusted per-task Thread identity carried by the Hook as
`session_id`, after Gate 1 proves it equals app-server `Thread.id`. App-server
`Thread.sessionId` is session-tree provenance shared by Forks and must never be
used as the authorization key. Repository/CWD and Decision space are evidence
within an Intent Epoch, not substitutes for Thread identity; two tasks in one
repository remain independent.

### 6.2 States and first-answer barrier

A Recall Session follows:

    disabled
      -> activating
           -> awaiting_product_clarification -> activating
           -> active
           -> blocked
      -> bypassed -> activating
      -> dormant -> revalidating -> active / blocked
      -> closed

A normal host `SessionEnd` means the current runtime stopped; it moves an
activated record to `dormant`, releases ephemeral work, and preserves the
user's same-Session authorization and Task Usage. Native `resume` of the same
trusted Session revalidates freshness and active revisions before the next
answer, then returns to `active` or `blocked` without creating another Intent
Epoch merely because the app was closed.

`closed` is reserved for an explicit user disable, a host-proven permanent task
deletion/archive event, or retention cleanup after that terminal proof. A Fork
and a genuinely new task receive new Session identities and start `disabled`.

`activating` is a hard ordering barrier:

1. persist trusted activation;
2. derive and validate `RecallIntent`;
3. resolve or clarify target Decision spaces;
4. validate snapshot/model readiness;
5. retrieve and classify a bounded shortlist;
6. atomically commit the epoch result and injection receipt; then
7. allow Codex to begin the affected answer.

A valid zero-match result may enter `active` with an empty set. A conflict may
enter `active` with blocked items, but the affected implementation cannot
continue until resolved. If the host cannot guarantee this barrier before the
activation answer, Packet 3 fails its Host Gate.

### 6.3 Bypass and re-enable

An activated Session enters `bypassed` only after an explicit native user
choice to continue that Session without historical Decisions. Session-wide
bypass clears the active set, emits one retirement marker for every previously
active revision still present in context, and disables recall gating for later
Turns. It does not treat expired Decisions as advisory context.

The user may explicitly re-enable later. Re-enable performs fresh routing,
freshness checks, and retrieval; it cannot revive an old set by toggling a flag.

## 7. Product and Decision-space routing

The user-facing word “product” maps to a publishable leaf `decision_space_id`:

- a real `product` leaf such as Cloud, ZNS, or ZMetis; or
- a concrete `shared_unit` such as
  `Shared / packages/shared / theme`.

Repository roots and catalog groups are not recall targets. `zstack-ui-next` is
a repository, not a product. `Shared` and intermediate directory groups cannot
supply Decisions.

Codex infers the target locally from the current Prompt, PRD, prior context,
signed repository mapping, code/path evidence, and domain vocabulary.
Repository evidence constrains the allowed catalog but is not the only signal,
which permits first-turn PRD development before a file is named.

Recall routing is ephemeral. A model-proposed target is an untrusted query hint
validated against the signed catalog; it cannot create a route, move ownership,
or affect Capture/publication.

The default is exactly one target leaf. `target_decision_space_ids` is an array
only to support a user who explicitly states that the current task develops
several products or Shared units. A monorepo alone never authorizes that.
Multi-leaf work shares the single eight-Decision/10,000-byte budget; it does not
receive a separate budget per product.

This is an intentional recall-specific narrowing of Capture routing. Capture
continues to create one trusted slice for every proven changed-path leaf in a
multi-leaf task. Recall uses the user's current development intent and does not
load several leaves merely because the repository or worktree contains them.

If one target cannot be determined confidently, ZDecision stops and asks the
user to clarify. It does not guess, silently continue, choose the repository as
a product, or load several possible products “just in case.”

Known parent-root and shared-worktree attribution gaps remain fail-closed only
when repository/path evidence is required to prove a route. An explicitly and
unambiguously named signed catalog leaf may still route a PRD-led recall without
that path proof. Model-supplied paths never become host proof for repository
ownership.

## 8. Recall Intent and intent epochs

Codex converts current local context into a typed `RecallIntent`:

    target_decision_space_ids
    explicit_multi_space
    feature_goal
    domain_objects
    repository_relative_paths
    constraints
    exclusions

The Agent validates sizes, normalizes terms and relative paths, rejects unknown
leaves, and derives an `intent_digest`. Raw Prompt or PRD bytes are not stored
as Recall Intent.

`UserPromptSubmit` is an opportunity for a local gate, not permission for heavy
retrieval after every Prompt. For an active Session, the Hook reads bounded
local state and supplies a small gate instruction. It performs no network
request, model invocation, transcript read, or ranking.

Every active native Turn receives a trusted local `turn_gate_id` bound to its
Session, Turn, current context epoch, Intent Epoch, and active generation. The
gate result must commit before the first substantive answer or any
command-executing/code-mutating tool call in that Turn. A scoped `PreToolUse`
backstop denies such a tool call while the current Turn gate is incomplete.
Missing, malformed, replayed-from-another-Turn, or invalid Turn Intent returns
`blocked`; it never permits Codex to keep using the previous set by default.

The Skill and Hook still cannot deterministically prevent arbitrary plain text
from being emitted before a tool call. Gate 1 therefore requires the current
Codex host to demonstrate that the injected developer context reliably orders
the recall gate before the visible development answer. If it cannot, Packet 3
stops at the Host Gate rather than claiming a weaker mode.

Codex supplies a bounded Turn Intent. The local gate returns:

- `reuse` for the same product(s), feature goal, module/path scope, constraints,
  and exclusions;
- `retrieve` when any of those semantic boundaries materially changes;
- `clarify_product` when routing is no longer unambiguous;
- `refresh_required` for explicit **重新检查决策** or mandatory lifecycle state;
  or
- `blocked` for unusable cache, model, security, or host state.

“继续,” tests of the same implementation, ordinary fixes, and refinements of
the same feature return `reuse`. The gate does not compare raw Prompt strings;
it compares normalized typed intent and explicit force-refresh state. The
same-intent local target is P95 below 50 ms.

The first successful retrieval creates `intent_epoch = 1`. A meaningful change
increments it and computes a replacement set:

- items applicable to both epochs remain active;
- old-feature-only items leave the set;
- new applicable items enter it; and
- a product change clears the old set before new routing.

Text already in model context cannot be erased. The Plugin emits a typed
retirement marker for revisions leaving the set; Codex must not apply them to
the new epoch. After compaction, only the current epoch's set is restored.

## 9. Local model and index lifecycle

Embedding and reranking are mandatory. ZDecision does not silently fall back to
keyword-only ranking when either is unavailable.

One versioned retrieval profile contains:

    retrieval_profile_id
    embedding model ID/revision and artifact/tokenizer digests
    reranker model ID/revision and artifact/tokenizer digests
    index_schema_version
    created_at

Model choice is evaluation-driven; this design freezes no model slug.
Candidate profiles compete on the same company-local bilingual benchmark.

Onboarding downloads and warms the approved local model pack in the background.
Artifacts are versioned and digest-verified. A new profile builds separate
indexes for the complete active snapshot and becomes active only after artifact,
tokenizer, dimension, schema, Decision-coverage, and query smoke checks pass.

The active profile remains available during a failed upgrade. Snapshot
generation, retrieval profile, and index generation are bound together, and
the pointer changes atomically only after full validation.

During first-use preparation, the user sees **正在准备决策召回**. ZDecision does
not claim an empty result or use a lower-quality fallback. The user may wait,
retry, or explicitly bypass recall for this Session.

## 10. Signed Decision distribution

### 10.1 Central source

Central constructs recall data only from an `available` verified Registry
projection. It never reads Git during a client recall request and never serves
Candidate, Review draft, unpublished preview, or private Publication data.

A signed current manifest contains at least:

    organization_id
    generation
    registry_tree_oid
    catalog_version
    leaves[]:
      decision_space_id
      compatibility_product_id
      decision_version
      snapshot_digest
      active-head manifest digest/count
    retrieval-profile manifest digest
    key_id
    issued_at
    expires_at

The referenced data contains the complete canonical formal active-head set,
document digests, ownership, lifecycle, and counts for each included leaf.
Active heads are available for new retrieval. Because the current V1 Registry
can produce only `revision: 1` and `lifecycle: active`, Packet 3 does not invent
revision, retirement, supersession, or invalidation records outside Git. When
an exact previously active `(decision_id, revision, digest)` is absent from a
newer signed complete active-head set, that signed removal immediately ends its
Session authority. The distribution and local transition types remain capable
of representing a future ordinary newer revision, but the Demo does not claim
that current V1 can produce or distinguish one.

A complete replacement snapshot is the correctness boundary. Cursor events and
notifications are wakeup hints only.

`generation`, the high-water mark, signature, LKG state, and freshness lease
are organization-wide for the first Demo. One generation maps every enabled
leaf to its exact Decision version and snapshot digest under one verified
Registry tree. Per-leaf progress may be built independently, and an unchanged
leaf may reuse identical verified blobs/indexes, but the organization active
pointer advances only after every enabled leaf in the manifest is ready. A
multi-leaf retrieval therefore freezes one generation plus the requested
leaf-to-version/digest subset, never a mixture assembled from unrelated active
generations.

Onboarding prefetches the signed catalog, current manifests, complete snapshots,
and retrieval indexes for every enabled leaf available to the Demo identity.
Readiness and generation watermarks are tracked per leaf. Registering or
enabling another leaf starts background prefetch before its first recall; if it
is not ready when selected, the activation shows `preparing` rather than a
false empty result. Later synchronization is incremental by generation and
Decision digest even though activation remains atomic at the complete-snapshot
boundary.

### 10.2 Trust root and canonical bytes

The Demo installs an owner-readable Ed25519 public trust root during onboarding.
The manifest names its `key_id` and signs bytes produced by the repository's
existing canonical JSON contract. Snapshot and model artifact digests use
SHA-256. Automatic signing-key rotation is deferred, but an unknown key fails
closed; there is no “accept first seen key” path.

Central may sign or extend a manifest only while its Registry projection is
`available` and bound to the verified Registry tree. A `syncing` or
`unavailable` projection is not empty and cannot extend freshness.

### 10.3 Monotonicity and rollback

The Agent stores the organization-level highest accepted `generation` and the
canonical digest of that complete manifest. It rejects:

- a lower generation;
- the same generation with a different digest;
- mismatched organization, catalog, tree, count, ownership, lifecycle, or
  document digests; and
- a snapshot that is partial or contains malformed formal bytes.

An authorized content rollback first creates a new reviewed Registry
publication/revert commit. Central then projects that new current Git tree and
publishes it as a higher generation even if some canonical Decision content
matches older bytes. Central cannot point a higher generation directly at
historical content outside the current available projection. A code-only Git
commit with the same Registry tree OID does not create new Decision content or
force reinjection.

### 10.4 Atomic last-known-good activation

New state is not visible until signature, manifest, complete bytes, catalog
ownership, model profile, and every index validate. The Agent then switches the
active generation in one local transaction. Readers see complete old or
complete new state, never a mixture.

If refresh fails, only the previous fully verified generation may serve as
last-known-good. Arbitrary history and partial builds are ineligible.

Local immutable Decision blobs referenced by active Sessions are retained by
`(decision_id, revision, digest, source_generation)` even after the global
cache pointer advances. A newly published Decision waits until the next Intent
Epoch. An exact active revision removed from the newer signed complete set loses
authority on the next Prompt. Future Registry formats may add an ordinary newer
revision branch, but V1 does not simulate it. Garbage collection waits until no
Session, LKG, or recovery record references the blob.

When the exact same `(decision_id, revision, digest)` remains active in a newer
generation, the Agent may rebind that active item to the newer signed lease
without reinjection. A removed revision cannot be rebound. If a future Registry
format supplies a non-invalidating replacement revision, the displaced pinned
revision remains bounded by its original source generation until the next
Intent Epoch; this future-compatible branch is not a V1 Demo claim.

### 10.5 Freshness and clock safety

| State | Recall behavior |
|---|---|
| `ready` | Complete verified generation within signed lease. |
| `degraded` | Refresh failed but signed LKG lease remains valid; use it with visible freshness. |
| `preparing` / `cold` | No fully indexed active generation; pause recall. |
| `expired` | Signed `expires_at` passed; stop treating old Decisions as authoritative. |
| `invalid` | Active bytes/indexes fail integrity; stop immediately. |
| `rollback_detected` | Reject incoming rollback; a valid LKG may continue only until its own expiry. |
| `clock_untrusted` | Local wall clock moved behind the last trusted signed-time boundary; do not extend or newly activate a lease. |

The client cannot extend `expires_at`. It stores the greatest trusted
`issued_at` plus a local monotonic deadline while running. A backward wall-clock
jump cannot increase remaining validity; after a restart with an untrustworthy
earlier clock, recall fails closed as `clock_untrusted` until trustworthy time
or a valid newer signed manifest is established.

The Demo initially checks for new state every five minutes in the background
and signs a maximum 24-hour offline lease. These are Central policy, not client
constants.

If the active generation expires during a Session, the next Prompt marks active
items invalid and pauses affected work. The user may retry or explicitly bypass
historical Decisions. Expired content is not silently advisory.

### 10.6 Publication and live Session changes

A completed publication advances the generation only after the verified
Registry projection contains that exact Registry tree. Prompt handling reads
local generation metadata; it does not synchronously fetch Central each Turn.

| Verified local event | Existing active Session behavior |
|---|---|
| Exact ID/revision/digest remains active | Rebind to the newer lease without reinjection. |
| Ordinary new Decision | Record pending generation; retrieve at the next Intent Epoch or explicit recheck. |
| Exact active revision is absent from the newer signed complete set | Remove on the first Prompt after the Agent activates that generation and display the change. |
| Future non-invalidating newer revision | Keep the pinned revision within its original lease and retrieve at the next Intent Epoch; not emitted by V1. |
| New generation cannot activate | Keep only an independently valid LKG until its own expiry; do not claim a revocation was seen. |
| Pinned source generation expires first | Remove its authority and pause affected work even if the Session intent did not change. |

No state changes in the middle of an executing Turn.

## 11. Hybrid retrieval

Decision embeddings are computed locally and incrementally by revision and
canonical digest. A query embedding is computed once per new Intent Epoch.
Codex never receives the full corpus.

The pipeline is:

    active lifecycle + allowed Decision-space hard filter
      -> BM25/full-text candidates
      + dense embedding candidates
      + exact path/scope/domain-object candidates
      -> bounded union
      -> Decision ID + revision + digest deduplication
      -> local cross-encoder reranker
      -> relevance threshold and byte budget
      -> bounded complete shortlist

BM25 and dense search independently contribute candidates; neither searches
only inside the other's results. Exact path/scope matching is a third channel.
Candidate depths, fusion weights, and thresholds belong to an approved
retrieval profile, not Prompt-controlled input. The reranker sees dozens, not
the corpus.

The final shortlist contains at most eight complete formal revisions and at
most 10,000 UTF-8 bytes across all selected leaves. A Decision is never
truncated. Zero items is valid and displays **没有找到与当前开发内容相关的正式决策**.

Codex evaluates only this bounded shortlist against the task and classifies each
as:

- `applicable`: governs current work and can be directly applied;
- `conflicting`: conflicts with current native requirements or another selected
  formal Decision; or
- `uncertain`: relevant, but scope or invalidation conditions cannot be proven.

Codex returns IDs, revisions, categories, and short local reasons. The Agent
validates them against the frozen shortlist/generation and atomically commits
the Intent Epoch, active set, blocked items, and receipt. Only `applicable`
items enter `active_injected_set`.

## 12. Application, conflict, and override

Applicable Decisions are supplied as complete typed envelopes before affected
implementation. The envelope includes stable ID, revision, digest, leaf,
claim, future action, scope, invalidation conditions, and minimal Registry
proof. No second per-item approval is required.

Two conflict classes are explicit:

- Decision versus Decision: ZDecision never chooses a winner. It pauses the
  overlapping work and asks the user to resolve or clarify scope.
- Decision versus current PRD/native user requirement: ZDecision shows both
  sides and lets the user follow the Decision, clarify applicability, or apply
  the current requirement locally.

Uncertainty also pauses only the affected portion and asks one focused question.

A user override is bound to
`(session_id, intent_epoch, decision_id, revision, digest)`. It permits this
Intent Epoch to proceed without that revision. It expires on Intent Epoch
change, Decision revision/digest change, signed invalidation, or Session end;
the new situation must be evaluated again. It cannot silently override every
Decision. Session-wide bypass is a separate explicit action.

The override emits a typed marker naming the excluded revision so earlier
envelope text is no longer treated as active within that epoch.

A local `DecisionDeviationEvent` stores only the bound identities, native
resolution Turn identity, resolution category, and timestamps. It stores no
Prompt or PRD excerpt. Packet 3 stores it as a local diagnostic only; the
existing two-stage Capture input, source freeze, and prompts do not consume it.
A later separately approved Capture amendment may use the pointer as a
non-evidentiary focus hint while still requiring native source confirmation.
The event itself never starts Capture, uploads a Candidate, or mutates Git.
Following the formal Decision creates no deviation event.

## 13. Active set, compaction, and Forks

`active_injected_set` contains only revisions classified applicable and
actually supplied to the current context, keyed by Decision ID, revision,
digest, leaf, source generation, and receipt.

An empty-match Prompt does not clear it. The same revision is not injected
again in one `context_epoch`. Failed classification/injection commits no set.

`SessionStart(source=compact|clear)` increments `context_epoch`, resolves the
current set against valid signed state, and restores surviving revisions once.
Restoration is idempotent by a trusted lifecycle-event identity or equivalent
local compaction key; it never depends on a model summary.

    relevant Prompt -> inject
    empty-match “继续” -> retain without injection
    compact/clear -> restore once
    next Prompt -> no repeat

`startup` and native `resume` of the same Session do not create a context
epoch. Normal `SessionEnd` moves the record to `dormant` but preserves
authorization and Task Usage; revalidation precedes the resumed answer. Only
the terminal `closed` conditions from section 6 clear authorization.

A Fork starts disabled. Parent Decision text or summary that the host copies
cannot always be physically erased, so it is marked `inherited_unverified` and
does not enter the child active set. The child must not apply it until the user
adds ZDecision again and freshness, routing, and applicability are revalidated.

The preferred host mechanism places Decision envelopes in a non-inherited
additional-context layer. When the host copies that layer, trusted
app-server parent/child task metadata or another supported host-owned fork fact
must identify the child; `SessionStart.source` cannot do so because its current
values are only `startup`, `resume`, `clear`, and `compact`. Once the relation
is proven, Session-start handling inserts a developer-level invalidation
envelope naming the inherited receipt IDs and child Session, while the local
gate and every receipt/application tool reject those IDs as inactive for the
child. A marker in ordinary assistant text is not sufficient.

The Host Gate must inspect the actual child context and prove that inherited
items cannot obtain an active receipt or govern a conflicting child task before
reactivation. If neither non-inheritance nor the trusted invalidation envelope
is supported reliably, Fork support fails rather than claiming isolation.

A subagent task is another Session and follows the same default-disabled rule;
the parent cannot delegate its recall authorization. Only a native user
activation in that task may enable it.

Internal Capture forks always disable recall. Inherited Decision envelopes are
reference context, not native user confirmation or Candidate evidence.
Inventory and Extraction explicitly exclude them as confirmation, preventing a
Decision feedback loop.

## 14. User-visible feedback

The first successful application in an epoch shows one expandable receipt:

    已应用「安恒」4 条正式决策

Expanded content includes titles, revisions, leaf name, short match reasons,
source generation, and freshness. It excludes scores, vectors, local paths,
Session IDs, and private source evidence.

V1 has no separate Decision `title` field. Packet 3 uses the canonical
`scope_summary` as `display_title`; it does not alter formal Registry bytes to
manufacture one.

Same-intent reuse shows no repeated receipt. A new receipt appears only when:

- a new Intent Epoch changes the set;
- compact/clear restores it;
- an active revision becomes invalid;
- conflict or uncertainty occurs;
- freshness changes to degraded/expired; or
- the user requests a recheck.

An empty result appears once per epoch. An unavailable result names the bounded
state (`preparing`, `expired`, `invalid`, or temporary refresh failure) and its
retry/bypass actions. ZDecision never claims application when only a Hook,
browser, or tool request was acknowledged.

## 15. Persistence and idempotency

The local private store owns:

- Recall Session activation/bypass/closure;
- Intent Epoch and normalized digest;
- context epoch and lifecycle event identity;
- frozen ranked shortlist;
- classification and blocked items;
- active set and receipts;
- signed generation high-water state;
- retrieval profile/index generation; and
- Decision-deviation events.

Activation, epoch retrieval/classification, and context restoration each have
stable local operation identities. Replay returns committed state rather than
injecting again.

A shortlist is frozen to one signed snapshot and retrieval profile. Retry may
reuse that exact valid shortlist but cannot adopt new Decisions or a new model
mid-operation. If the generation becomes invalid before classification commits,
the operation stops and retrieves again.

Recall state never enters Git or Central.

## 16. Failure behavior

| Condition | Required behavior |
|---|---|
| Plugin not selected | No recall work or state. |
| Product leaf ambiguous | Clarify; retrieve nothing. |
| Unknown/disabled leaf | Bounded unavailable result. |
| Central offline with valid signed LKG | Use degraded LKG and display freshness. |
| Central offline without valid LKG | Pause; allow retry or explicit Session bypass. |
| Model/index preparing | Display preparation; no keyword-only fallback. |
| Incoming signature/digest/generation invalid | Reject; retain only independently valid LKG. |
| Active cache expired/corrupt | Remove authority and pause affected work. |
| Retrieval returns zero | Commit a valid empty epoch and continue. |
| Conflict/uncertainty | Pause affected work and ask. |
| Tool response lost | Reconcile stable operation; do not inject twice. |
| Restoration replayed | Return receipt; do not restore twice. |
| Fork not provable | New unactivated Session; no inherited authorization. |
| Pre-answer injection not supported | Fail Host Gate. |

An unavailable service never blocks a Session that did not opt in. In an
activated Session it blocks only affected development until retry or explicit
bypass. This is the explicit Packet 3 exception to the older rule that Plugin,
Agent, network, or cache failure never blocks ordinary development. Candidate
refresh and every unactivated Session preserve that older non-blocking rule.

## 17. Evaluation contract

A private versioned benchmark covers Chinese, English, mixed-language,
product/Shared work, PRDs, conversational Prompts, paths, constraints,
same-product hard negatives, cross-product homonyms, ambiguity, conflict,
uncertainty, no-match, lifecycle changes, first-turn activation, mid-session
activation, intent changes, and explicit multi-product tasks.

Each run freezes:

- benchmark version and digest;
- organization manifest generation/digest and requested leaf snapshot digests;
- catalog version;
- retrieval profile/model artifacts;
- target Mac hardware and OS;
- warm/cold cache condition; and
- score definitions and thresholds.

Initial gates are:

| Metric | Definition | Gate |
|---|---|---:|
| Unambiguous leaf routing accuracy | exact correct leaf on labeled unambiguous cases | at least 95% |
| Wrong-leaf retrieval | any retrieval from a non-gold leaf | 0 in acceptance set |
| Ambiguity safety | labeled ambiguous case retrieves nothing and asks | 100% |
| Unnecessary clarification | unambiguous cases that ask | at most 10% |
| Candidate Recall@20 | gold relevant items found in the pre-final top 20; positive cases only | at least 95% |
| Final Precision@8 | pooled relevant/applicable returned items divided by all returned items; empty output adds no precision denominator | at least 90% |
| Final applicable Recall@8 | gold applicable items entering the active set divided by gold applicable items; positive cases only | at least 85% |
| Applicability classification | macro-F1 across applicable/conflicting/uncertain | at least 90% |
| Blocking-conflict false negative | labeled blocking conflict classified applicable or omitted after retrieval | 0 in safety set |
| Positive-query abstention | empty final output on a query with gold applicable items | counts as a Final Recall@8 miss |
| No-match correctness | labeled no-match case injects nothing | 100% |
| Retired/invalid injection | any lifecycle-invalid injected revision | 0 |
| Warm local retrieval P95 | ready model/index, excludes cold download | at most 800 ms |
| Same-intent gate P95 | local gate only | at most 50 ms |
| Warm end-to-end added P95 | activation/gate through receipt, above normal Codex answer latency | at most 3 s |

These are launch gates, not runtime constants. Direct application favors final
precision over filling eight slots. Sensitive benchmark data stays within the
company environment.

## 18. Implementation and acceptance gates

### Gate 1: Codex host capability

Before retrieval implementation, prove in real Codex Desktop:

1. native first-Turn selection binds the correct Session;
2. affected development waits behind the first-answer barrier;
3. a later Turn activates using retained context;
4. every later active Turn binds and commits its gate before substantive output
   or a command-executing/code-mutating tool, while an invalid/replayed gate
   fails closed;
5. compact/clear restores typed context idempotently;
6. the Hook `session_id` equals exact app-server `Thread.id`, never the shared
   tree-level `Thread.sessionId`, and structured active-Turn input proves the
   native ZDecision selection;
7. supported app-server/host facts identify a Fork without inventing a
   `SessionStart` source, and inherited content stays inactive through context
   inspection plus receipt/tool rejection; and
8. Capture forks remain recall-disabled and exclude inherited Decision
   envelopes as confirmation.

Failure stops Packet 3 and redesigns only host integration. Do not build the
model stack or distribution path around an unproven assumption.

### Gate 2: Trusted local data and model loop

- A clean device performs real onboarding prefetch without seeded cache.
- Signature, canonical bytes, complete snapshot, high-water, clock rollback,
  LKG, expiry, and invalidation cases pass.
- Missing/partial/corrupt/rollback states never activate.
- Model artifacts and complete indexes validate before atomic activation.
- Failed upgrades preserve the old valid profile.
- Publication advances generation only after the verified projection contains
  its exact Registry tree.
- newly published Decisions wait for the next Intent Epoch, while removal from
  a newer signed complete active-head set immediately ends authority;
- the future ordinary-revision branch is type-tested but is not claimed as a
  current V1 producer capability.

### Gate 3: Offline retrieval quality

- Run the frozen benchmark without Codex UI.
- Prove independent BM25, dense, and path channels.
- Prove bounded union, deduplication, reranking, thresholds, and complete-item
  budgeting.
- Meet every routing, relevance, lifecycle, and latency gate.
- If quality misses, change only retrieval/index/profile behavior; do not expand
  Central UI or lifecycle scope.

### Gate 4: Integrated real development

Acceptance covers:

1. no Plugin selection: no recall/Task Usage;
2. first-Turn PRD activation before answer;
3. mid-session activation from prior context;
4. every active Turn completes its bound gate before substantive output or a
   command-executing/code-mutating tool;
5. ambiguity and explicit multi-product routing;
6. same-intent reuse and intent-change replacement;
7. conflict, uncertainty, per-Decision override, and Session bypass;
8. empty “继续” followed by one compact restoration;
9. new-Decision deferral versus immediate signed active-head removal;
10. normal SessionEnd followed by native resume preserves activation but
    revalidates freshness;
11. valid offline LKG, expiry, corruption, and clock rollback;
12. Fork unverified inheritance and reactivation;
13. Capture-fork feedback-loop prevention; and
14. network proof that no Prompt, PRD, source, paths, vectors, scores, native
    IDs, or recall state reached Central.

## 19. Boundary and stop rule

Packet 3 may add only:

- native per-Session activation and bypass;
- bounded recall lifecycle Hooks/tools;
- local Recall Intent, Intent Epoch, context epoch, active set, receipts, and
  deviation records;
- signed complete formal-Decision distribution from the verified read model;
- local model lifecycle, indexes, hybrid retrieval, and reranking;
- bounded applicability classification and Decision envelopes; and
- the narrow Capture-prompt exclusion that prevents inherited Decision
  envelopes from becoming confirmation evidence; and
- recall/conflict/freshness/restoration feedback in Codex.

It must not add automatic Candidate generation, a cloud query service, central
Prompt storage, Registry V2, Decision-revision UI, production OIDC/SSO, a task
scheduler, generic memory, or unrelated Central Web redesign.

After written approval and before Packet 3 is claimed implemented,
`docs/architecture.md`, `README.md`, `AGENTS.md`, and the installed Plugin Skill
must be aligned to say **Session-opt-in Decision recall**. Historical text that
says every new Session receives Decisions automatically must not remain an
active product instruction.

Implement one Gate at a time. Each Gate permits focused correction of confirmed
blocking defects. After Gate 4, run one focused suite, one complete suite, and
one bounded real Codex Desktop acceptance. Record non-blocking improvements and
stop. Do not begin another broad architecture audit, Skill blind test, or
unbounded hardening loop.
