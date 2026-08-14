# Stored Session Capture Evidence Design

**Status:** Proposed for user review on 2026-08-14.

**Scope:** Candidate refresh for a user-selected stored Codex task when durable
decisions may predate ZDecision's local Hook observation ledger. This design
does not change Recall authorization, automatic Capture, Review, or
publication authority.

**Amends after approval:** Sections 2–6 and 9–12 of
`2026-08-07-recall-capture-provenance-design.md`, the Capture provenance and
privacy portions of `docs/architecture.md`, and the Inventory/Extraction
protocol revision. Clauses not explicitly amended remain authoritative.

## 1. Problem and decision

The current Candidate Capture protocol has two different visibility windows:

1. a Capture fork can inherit the complete stored Codex task history; but
2. ZDecision can issue qualifying receipts only for user prompts observed by
   its Hooks after the Plugin began recording that task.

When a durable decision appears before the first Hook event, the model can see
the decision but cannot cite its real source. Requiring a receipt anyway can
cause the model to attach an unrelated later receipt. A semantic verifier can
then reject a valid decision, or—worse—accept the wrong evidence relation.
Changing the handled cursor cannot repair this: the cursor can revisit only
evidence that was already recorded; it cannot manufacture historical Hook
events.

ZDecision therefore adds a second host-owned evidence source for an explicit
Candidate refresh:

`stored_user_message_anchor`

The local Agent reads the selected stored task through the supported Codex
App Server API, freezes eligible text-only `userMessage` items through an exact
completed Turn, and issues opaque receipts bound to those immutable message
digests. The model receives an explicit, bounded receipt-to-message catalog in
an **ephemeral** internal Capture fork. Raw message text is not persisted by
ZDecision and never crosses into Central.

Hook-observed prompt anchors remain the preferred incremental path. Stored
message anchors fill only the otherwise unprovable part of the selected
history. Identical underlying messages are deduplicated and never receive two
independent qualifying receipts.

This design improves evidence attribution. It still does not make model
semantic judgment a publication authority: Candidate Review and exact
publication confirmation remain mandatory.

## 2. Supported host contract

The implementation uses only documented App Server operations:

- `thread/read` with `includeTurns: true` reads a stored task and does not
  resume or subscribe to it;
- `thread/fork` with `lastTurnId` freezes inherited history through one Turn;
  and
- `thread/fork` with `ephemeral: true` creates an in-memory fork that is not
  added to stored task listings.

The source is the user-selected interactive task. The Agent must not parse
rollout JSONL, search transcript filenames, infer a task by recency, resume the
source task, or start a second conversation runtime as a substitute.

`thread/turns/list` and `thread/items/list` are experimental and are not part
of this contract. If `thread/read(includeTurns=true)` cannot return the full
stored history—for example, for an unsupported paginated record—the source
fails closed as `historical_evidence_unavailable`.

An ephemeral fork of a paginated task requires an experimental option. This
design does not enable that option or silently fall back to a persisted fork.

This use is strictly post-Turn Candidate Capture. `thread/read` is not used as
an active-Turn synchronization broker, a Recall consent proof, or a mutation
gate.

Official contract:
[Codex App Server — Threads](https://learn.chatgpt.com/docs/app-server#threads).

## 3. Source boundary and eligibility

The explicit **当前 Session** or **所有有效 Session** action is the only
authority to inspect stored history. Background lifecycle Hooks do not trigger
historical inspection.

For each selected source, the Agent freezes:

```text
StoredHistoryBoundary
  version = 1
  repository_id
  source_thread_id                 # private
  lower_turn_id_exclusive          # optional committed checkpoint
  upper_turn_id_inclusive          # exact completed source Turn
  upper_stop_event_id              # exact explicit-refresh boundary
  source_cwd_binding               # private
  ordered_turn_digest
```

Only items satisfying every rule are eligible:

- the parent task is the exact selected interactive source;
- the Turn is completed and lies inside the frozen lower/upper window;
- the item type is exactly `userMessage`;
- its content is a non-empty list containing only supported `text` inputs;
- its normalized text is non-empty and within configured per-message and
  per-source byte caps; and
- the task is not registered as a Capture, reconciliation, Recall-internal,
  review, or other ZDecision-owned task.

Assistant messages, reasoning, plans, Hook prompts, summaries, tool calls,
diffs, commands, files, app resources, images, local images, and Capture
artifacts do not qualify. A mixed or unsupported
`userMessage` item is retained only as a nonqualifying diagnostic count; it
does not receive a receipt.

The App Server `userMessage` type does not prove that a physical human authored
the input. The UI must continue to say **存储任务中的用户消息已校验**, not
**已证明来自用户本人**.

The host also cannot infer a physical-user origin bit for a text-only
`userMessage` when the App Server does not provide one. Such a message may
receive a stored-message receipt, but control messages and continuations still
have to pass the same semantic durability checks and human Review.

## 4. Canonical evidence records

The host creates one canonical record for each eligible message:

```text
StoredMessageAnchor
  receipt_id
  source_kind = stored_user_message_anchor
  source_thread_id                 # private
  turn_id                          # private
  item_id                          # private
  turn_ordinal
  message_ordinal
  normalized_text_digest
  active_reference_set_digest     # optional host-owned Recall lineage
```

`normalized_text_digest` binds canonical JSON containing the exact ordered
list of text-block strings. The host validates UTF-8 and byte limits but does
not apply Unicode normalization, trim content, or collapse whitespace before
hashing. The catalog renders the same ordered blocks with an unambiguous fixed
separator.

The raw text is not part of the durable record. `receipt_id` is opaque and
host-issued. A receipt is valid only inside the exact repository, source task,
boundary, manifest, and Capture operation that issued it.

If a Hook-observed anchor and a stored-message anchor identify the same exact
`userMessage` item and normalized text digest, the manifest contains one
canonical anchor. The host preserves the Hook event as an observation facet,
but the model receives only one receipt. Any item/digest disagreement fails
the source rather than minting parallel evidence.

The new immutable manifest is versioned separately:

```text
CaptureEvidenceManifestV2
  version = 2
  source_session_id
  lower_turn_id_exclusive
  upper_turn_id_inclusive
  upper_stop_event_id
  anchors[] = CanonicalMessageAnchor {
    receipt_id,
    authority_kind = host_observed_user_message,
    source_facets[] = hook_observed | stored_history,
    private coordinates, ordinals, content digest, Recall lineage digest
  }
  ordered_turn_digest
  manifest_digest
```

Persisted fields contain only private coordinates, opaque receipts, digests,
ordinals, source-kind/facet metadata, and Recall lineage digests. They contain
no message text, excerpt, paraphrase, attachment, command, file path, or model
summary.

## 5. Transient evidence catalog

The host must give the model an unambiguous association between each receipt
and the message it represents. An enum of receipt IDs without this association
is insufficient and is forbidden in the V2 protocol.

After re-reading and verifying the frozen source, the Agent creates an
ephemeral fork through `upper_turn_id_inclusive`, registers it as an internal
Capture task before the first structured Turn, and supplies a bounded catalog:

```text
ZDECISION_STORED_MESSAGE_EVIDENCE_CATALOG
  manifest_digest
  records[] = {
    receipt_id,
    message_ordinal,
    text
  }
END_ZDECISION_STORED_MESSAGE_EVIDENCE_CATALOG
```

The catalog is explicitly untrusted business data. Receipt membership,
ordering, and digest validation remain host-owned. The model cannot add a
record or change which message a receipt names.

The ephemeral fork exists only in the controlled App Server process. It is not
added to stored task listings and is never archived as a persistent Capture
task. On success or failure, the client closes the in-memory fork/process. A
crash loses the ephemeral text and is safe to retry.

Retry performs a fresh `thread/read`, reconstructs the same normalized records,
and requires every item identity, ordinal, digest, boundary, and manifest
digest to match the frozen durable manifest before creating a new ephemeral
fork. Missing, changed, reordered, duplicated, or newly inserted source data
fails closed. A frozen operation never grows to include later messages.

If the host cannot create the ephemeral fork, it must not put the catalog in a
persistent disposable task. It returns `historical_evidence_unavailable`.

## 6. Evidence-first model protocol

### 6.1 Inventory

Inventory receives the complete bounded catalog and returns one atomic signal
with a non-empty, canonical list of receipt IDs for every
`current_confirmed` result.

Multiple receipts may collectively support one durable rule. The verifier must
evaluate the selected messages as one ordered evidence set; it must not require
every individual message to restate the whole rule. This supports normal
conversation patterns such as proposal, correction, and final confirmation.

Deterministic validation requires:

- every selected receipt belongs to the frozen V2 manifest;
- receipt IDs are unique and remain in manifest order;
- every selected receipt resolves to the exact catalog record used in this
  attempt;
- an unselected historical message, inherited assistant text, or later Hook
  prompt cannot supply missing evidence.

One source message may legitimately state more than one durable rule, so a
receipt may support multiple independently atomic signals. The complete
receipt set for each signal is still validated separately and cannot be
changed by Extraction.

The model can still choose a semantically unrelated valid message. That is a
quality error, not a host-authentication bypass, and Stage 2 plus human Review
remain required.

### 6.2 Extraction and semantic audit

Extraction's authoritative evidence input for each eligible signal is:

- the validated signal fields;
- that signal's selected catalog records;
- the fixed Decision template and Decision-space route; and
- relevant known-gap records with explicit relation metadata.

The inherited ephemeral fork history remains visible as nonqualifying
reference context; it cannot supply missing evidence. Extraction returns one
review for every eligible signal and at most one Candidate per signal. It
cannot author or modify receipt IDs.

The semantic audit evaluates the selected records **collectively** and uses
the following distinction:

- `decision_core_gap`: contradicts or leaves unresolved the durable rule,
  future behavior, product boundary, or required invariant; this may veto the
  Candidate.
- `implementation_detail_gap`: concerns a task list, UI polish, file layout,
  migration step, test detail, or other implementation work that does not
  invalidate the durable rule; this must not veto an otherwise supported
  Candidate.

An unrelated known gap cannot reject a signal merely because it appears later
in the task. The extraction result must identify which signal ordinal a gap
affects and how. Unknown relation or contradictory evidence routes to
`needs_evidence`, not to a fabricated Candidate.

### 6.3 Provenance

Eligible Candidates use:

```text
CandidateProvenanceV2
  version = 2
  kind = verified_user_message_anchor
  manifest_digest
  source_signal_ordinal
  evidence_receipt_ids[]
  evidence_source_facets[]         # private, aligned with receipts
  reference_decision_ids[]         # host-derived private lineage
  disposition
  provenance_digest
```

Mixed Hook/stored facets for one canonical message do not create mixed
Candidate authority. The local Candidate records the unchanged receipt set and
host-derived facets. Central continues to receive only the existing minimized
`candidate-provenance-v1 / host_observed_user_prompt_anchor / digest` summary;
the broader label remains accurate because the local host observed and bound
every qualifying message. Central never receives receipts, facets, or source
coordinates.

## 7. Failure and UI semantics

The workflow distinguishes these outcomes:

| Condition | Result |
|---|---|
| Full frozen history and ephemeral catalog available; no durable signal | `no_new_candidates` |
| History missing, paginated/unsupported, malformed, over limit, or changed on retry | `historical_evidence_unavailable` |
| Eligible signals exist but semantic relation is unresolved | `needs_evidence` with bounded counts |
| Candidate persisted | existing synchronized Candidate count |

**当前 Session** surfaces the exact bounded source result. **所有有效
Session** continues other valid sources but reports how many sources were
unavailable. The card must not collapse `historical_evidence_unavailable` into
**没有发现新的候选决策**.

Logs and cards may show only sanitized state, counts, source ordinal, and
bounded digest prefixes. They do not show raw messages, task/Turn/item IDs,
local paths, catalog text, or model output.

## 8. Privacy, retention, and trust boundaries

- Raw source messages remain in their original Codex task and transiently in
  one in-memory Capture fork.
- ZDecision's SQLite stores no raw message, excerpt, paraphrase, or searchable
  prompt archive. V2 deliberately adds one SHA-256 integrity digest of the
  canonical text-block list inside the private immutable operation record;
  that digest is used only for retry equality and provenance integrity.
- Central, Registry, Git, Candidate payloads, logs, status output, and
  acceptance reports receive no raw messages, receipts, task/Turn/item IDs, or
  local paths.
- Capture/reconciliation tasks remain Recall-disabled before their first Turn.
- The ephemeral catalog is not Recall authority and cannot release a mutation
  gate.
- Review and exact publication confirmation remain the only business and
  publication authorities.

## 9. Compatibility and migration

This is a new Capture protocol revision. Existing immutable operations retain
their original bytes and semantics.

- Completed V1 Hook-manifest operations remain readable and are not upgraded.
- In-flight V1 operations resume only under their frozen protocol.
- New explicit refresh requests use V2 when stored-history capability is
  available.
- A V2 failure never falls back to a receipt-only prompt, raw rollout parsing,
  a persisted catalog, keyword filtering, or model-authored source identity.
- Hook-observed incremental evidence continues to work and is merged into the
  canonical V2 manifest without duplicate receipts.
- Candidate IDs, Review records, publication records, formal Decisions, and
  Registry schemas do not change in this slice.

## 10. Rejected alternatives

### Cursor rewind

Rejected. It revisits recorded evidence but cannot recreate Hook events that
never existed.

### Let the model cite any visible history and trust its receipt choice

Rejected. This caused the observed unrelated-later-receipt failure and is not
auditable.

### Persist a raw transcript or prompt archive in ZDecision

Rejected. It expands the private-data boundary and duplicates source content.

### Put the catalog in the existing persisted Capture fork

Rejected. It creates another durable copy of private task text. Historical V2
requires an ephemeral fork or fails closed.

### Parse rollout JSONL or use transcript filenames

Rejected. Those are private implementation formats, not the supported App
Server contract.

### Add product- or feature-specific extraction rules

Rejected. The fix must work for any durable decision conversation and cannot
name Anheng, SSO, ZIAM, or another one-off feature in production logic or test
oracles.

## 11. Acceptance matrix

Automated tests must prove:

1. a durable decision stated before the first Hook event receives a valid
   stored-message receipt and can produce one Candidate;
2. an unrelated later Hook-observed message cannot be substituted for that
   historical evidence;
3. proposal + correction + confirmation receipts collectively support one
   atomic signal;
4. each receipt is paired with the exact source message shown to Inventory and
   Extraction;
5. duplicate Hook/stored observations of one message yield one receipt;
6. source messages after the frozen upper Turn are excluded;
7. retry reconstructs identical records and rejects item, order, digest, or
   boundary changes;
8. missing history, unsupported pagination, mixed/non-text user content, and
   byte-cap overflow fail with the correct explicit bounded outcome;
9. assistant, tool, code, recalled Decision, summary, and Capture artifacts
   receive no stored-message receipt;
10. an implementation-only known gap cannot veto a supported durable rule,
    while a core contradiction does;
11. V1 Hook-only operations remain byte-compatible and V2 never falls back to
    V1 after validation failure;
12. raw source text is absent from the Agent/Capture/Recall databases, logs,
    Candidate/Central/Registry payloads, Git, and persisted disposable tasks;
13. the ephemeral Capture task is registered Recall-disabled before its first
    structured Turn and leaves no stored task after completion;
14. ordinary Recall confirmation, delivery, application, and mutation gates do
    not call `thread/read`; and
15. tests use generic proposal/correction/confirmation examples and contain no
    production feature-specific oracle.

Real Desktop acceptance additionally proves one old task whose durable
decision predates Hook observation, one current Hook-observed task, crash/retry
with stable digests, and absence of a persisted evidence-catalog task.

## 12. Hard-stop rule

Implementation stops if the active Desktop/App Server cannot provide all of:

- complete stored Turns through an exact completed upper boundary;
- stable `userMessage` item identity and canonical text content on repeat read;
- an in-memory fork that does not enter stored task listings; and
- deterministic retry verification without persisting raw source text.

When any requirement is absent, ZDecision reports
`historical_evidence_unavailable`. It does not weaken the evidence firewall,
invent receipts, broaden model authority, or silently claim that no Candidate
exists.
