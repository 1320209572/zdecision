# Recall-to-Capture Provenance Firewall Design

**Status:** Approved for implementation planning on 2026-08-07.

**Scope:** The narrow Packet 3 boundary that prevents recalled formal Decisions
from silently qualifying themselves as new Candidate evidence when the user
later runs the existing, explicit Candidate refresh workflow.

**Amends after approval:** Section 4.1 of `docs/architecture.md`; sections 4,
13, 18 Gate 1 item 8, and 19 of
`2026-08-06-session-opt-in-intelligent-decision-recall-design.md`; and Task 7
of `2026-08-06-recall-host-gate.md`.

This amendment does not make Candidate Capture automatic. The repository page
or inline **当前 Session** / **所有有效 Session** action remains the only
Capture start boundary. Review and the later exact **确认发布** action remain
the only publication authorities.

## 1. Problem and decision

Recall places reviewed formal Decision text inside a native development
Session. A later Capture fork inherits that context together with native user
messages, assistant proposals, tool results, and possible compaction content.
The current `Inventory -> Extraction -> Candidate -> Reconciliation` protocol
does not retain a host-verifiable distinction between those sources.

Consequently, prompt wording or marker filtering cannot provide the required
boundary. A model can label a recalled envelope as
`explicit_user_direction`, and the current validators cannot distinguish that
claim from independent user support. Business-topic or marker filtering would
also delete legitimate native conclusions that happen to discuss the same
rule.

ZDecision therefore adopts an evidence-eligibility firewall:

1. a trusted local Hook ledger is the only issuer of prompt-event anchors;
2. recalled Decisions, assistant text, tool output, code state, Capture output,
   and compaction summaries are reference context and never issue anchors;
3. Inventory selects only host-issued opaque anchor IDs from a frozen
   allowlist before Extraction generates Candidate content;
4. every Candidate is bound to one validated Inventory signal and its exact
   anchor set;
5. reconciliation may preserve provenance but cannot create or replace it;
6. the semantic Capture stage routes recognized reference-only and unresolved
   mixed cases away from the ordinary Central Candidate Inbox; and
7. a human Review and the existing exact publication confirmation remain the
   semantic and publication authority.

This design proves source eligibility and chain integrity. It does **not**
claim that the host can prove a natural-language conclusion was causally or
logically derived from a particular prompt. Model citation is a quality aid;
it is not publication authority.

## 2. Guarantees and non-guarantees

### 2.1 Guaranteed by deterministic code

- A model cannot invent a qualifying evidence ID.
- A recalled Decision envelope cannot issue a qualifying evidence ID or pass
  host validation without some Hook-observed prompt anchor from the frozen
  source window.
- Every new-protocol Candidate names one Inventory signal whose complete
  qualifying anchor set belongs to the frozen source boundary.
- A missing, duplicated, reordered, cross-Session, post-boundary, or unknown
  anchor invalidates the complete extraction attempt.
- Candidate provenance is included in local result and reconciliation digests.
- Reconciliation cannot convert reference context into new provenance.
- No raw Prompt, transcript, evidence excerpt, Session ID, Turn ID, local path,
  or receipt ID enters Central.
- A Candidate still cannot publish without the existing user Review, immutable
  preview, and exact **确认发布** Turn.

### 2.2 Deliberately not claimed

- A Hook event does not by itself prove a physical human authored the input.
  The supported `UserPromptSubmit` contract contains the Session, Turn, and
  Prompt event, but no documented `native_human` origin bit.
- An anchor proves that a Hook-observed prompt event exists; it does not prove
  the Candidate is semantically entailed by that prompt.
- A model can still select a valid but semantically unrelated anchor; for
  example, it can incorrectly attach the anchor for **继续** to a rule that
  appeared only inside a recalled Decision. The
  evidence-first contract, conservative disposition rules, and optional
  semantic verifier reduce that error, while human Review remains the only
  authority that can accept the business meaning.
- `confirmation_basis`, a model-generated citation, an entailment score, or a
  second model cannot become a security or publication boundary.
- Code, tests, commits, pushes, and tool output can help route or scope a
  Candidate but cannot alone prove that the user adopted a durable decision.

The UI and documentation must therefore say **本地来源锚点已校验**, never
**已证明来自用户原话**.

## 3. Source types

The host owns source classification. The model receives opaque IDs and cannot
author or change source types.

| Source type | May qualify a new Candidate? | Role |
|---|---:|---|
| `hook_observed_user_prompt_anchor` | Yes, subject to model selection and Review | Bounded evidence anchor |
| `recalled_decision` | No | Reference and existing-Decision lineage |
| `assistant_proposal` | No by itself | The object of an explicit later confirmation |
| `code_or_tool_fact` | No by itself | Routing, scope, and validation context |
| `hook_or_system_context` | No | Non-native control/reference context |
| `capture_artifact` | No | Inventory, Extraction, and reconciliation output |
| `compaction_or_summary` | No by itself | Retained context; may point to still-valid anchors |
| `unknown_or_legacy` | No in the new protocol | Fail-closed compatibility state |

Two texts with identical bytes keep different authority when their host source
types differ. A native prompt anchor can qualify; the identical bytes inside a
formal Decision envelope cannot.

## 4. Host-issued prompt anchors

The existing local Hook event ledger remains the privacy boundary. ZDecision
does not add a Prompt-text, transcript, excerpt, or Prompt-hash archive.

For each eligible `UserPromptSubmit` event already observed locally, the host
can derive an opaque receipt with at least:

```text
PromptAnchorReceipt
  receipt_id
  repository_id
  session_id
  turn_id
  cwd_binding
  hook_event_id
  observed_at
```

The receipt is never model-authored. It is valid only when it belongs to the
same registered and enabled repository, source Session, and frozen Capture
boundary. A receipt created after that boundary is not eligible on retry.

At source freeze, the local Agent stores a bounded, ordered
`CaptureEvidenceManifest`:

```text
CaptureEvidenceManifest
  version = 1
  kind = hook_observed_user_prompt_anchor
  source_session_id
  lower_turn_id_exclusive
  upper_turn_id_inclusive
  boundary_event_id
  anchors[] = (receipt_id, turn_id, anchor_ordinal,
               active_reference_set_digest)
  manifest_digest
```

Only IDs and host metadata are persisted. Raw Prompt content remains where it
already exists in the private Codex Session and is not copied into ZDecision's
Private Store or Central.

Only anchors in the existing Capture window
`previous_handled_turn_id < anchor.turn_id <= upper_turn_id` are eligible. The
lower and upper boundaries are frozen with the ordered anchors. This prevents
a later refresh from reusing a prompt that belonged to an already handled
window.

The manifest is immutable for one Capture operation. Retry, crash recovery,
and disposable Capture replacement reuse the same bytes. A continued source
Session creates newer Hook events but cannot enlarge an already frozen
manifest.

If no qualifying manifest can be frozen, that source is excluded with a
sanitized local reason. **所有有效 Session** continues with other valid
sources; **当前 Session** returns an explicit no-eligible-source result. The
system does not fork, extract, retry indefinitely, or substitute CWD, recency,
transcript filenames, marker text, or model claims.

## 5. Evidence-first Capture protocol

The new protocol is versioned separately from existing `extractor-v4`
operations. Existing immutable operations are never reinterpreted as having
provenance they did not record.

The stronger theoretical alternative is to give Stage 1 a temporary packet
containing only raw native Prompt text and physically exclude recalled,
assistant, tool, and summary context. That would prevent an unrelated anchor
from borrowing recalled text. The current Plugin does not select this path:
supported app-server Turns are stored task history, so replaying raw Prompts
into a new disposable task would duplicate private source text in another
persistent transcript, and the public host contract does not expose an
ephemeral, non-persisted structured-generation channel. A future custom Codex
host could own that input channel. Task 7 must not emulate it with rollout-file
parsing or a second conversation runtime.

If the real Host Gate discovers a supported source-isolated, non-persisting
input channel, a later amendment may strengthen Stage 1. The approved V1
assurance remains host-verified anchor eligibility plus semantic filtering and
human Review.

### 5.1 Stage 1: Inventory

The host supplies a bounded enum of opaque receipt IDs from the frozen
manifest. Every Inventory signal returns:

```text
signal_ordinal
topic
rule
future_effect
scope
status
confirmation_basis
confidence
evidence_receipt_ids[]
```

`confirmation_basis` remains useful for business interpretation but is not an
authority field. Deterministic validation requires:

- `evidence_receipt_ids` is non-empty for `current_confirmed`;
- every ID belongs to the frozen manifest;
- IDs are unique and in canonical manifest order;
- an envelope, receipt marker printed in text, assistant message, tool output,
  or Capture artifact cannot add an ID to the manifest; and
- the host derives recalled-Decision lineage from the frozen Recall state of
  each selected receipt Turn; the model cannot add, remove, or rewrite that
  lineage.

Unresolved, superseded, reference-only, and source-uncertain signals may be
recorded for coverage inside the private operation, but Extraction cannot turn
them into Candidates.

### 5.2 Stage 2: Extraction

Extraction still converts one signal into at most one Candidate. It additionally
returns that signal's ordinal. The host, not the model, constructs:

```text
CandidateProvenance
  version = 1
  kind = hook_observed_user_prompt_anchor
  manifest_digest
  source_signal_ordinal
  evidence_receipt_ids[]
  reference_decision_ids[]  # host-derived union for selected receipt Turns
  disposition
  provenance_digest
```

The Candidate's receipt IDs must exactly equal the validated signal's IDs.
Extraction cannot add, remove, reorder, or replace them. The source checkpoint
and manifest digest must equal the frozen operation. Any mismatch invalidates
the complete attempt before Candidate persistence.

Prompt contracts use source types and opaque IDs to improve model behavior,
but host validation—not prompt text—is the boundary.

## 6. Candidate dispositions

Only `candidate_eligible` enters reconciliation and the ordinary Central
Candidate Inbox. The host determines structural eligibility; the fixed model
contract determines business meaning and semantic disposition. The latter is
a high-precision quality gate, not a proof. A false-positive semantic
classification can still reach Review but cannot publish itself.

| Observed case | Disposition | Ordinary Inbox |
|---|---|---:|
| User prompt explicitly states a durable rule | `candidate_eligible` | Yes |
| User explicitly confirms a precisely identified assistant proposal from before Recall activation | `candidate_eligible` with `adopted_decision_contract` | Yes |
| Host has an explicit local application receipt for an already recalled formal Decision | `existing_decision_adoption` | No new Candidate |
| Only a recalled Decision, recall receipt, or host probe supports the rule | `excluded_reference_only` | No |
| Assistant proposal may depend on recalled Decisions and the user only gives a short assent | `needs_evidence` | No |
| Native and recalled sources cannot be separated for one atomic rule | `needs_evidence` | No |
| Only code, tests, commit, push, or tool output supports the rule | `excluded_code_fact_only` | No |
| Source identity or manifest validation fails | `excluded_unverified` or failed attempt | No |

For a short assent such as **认可**, **可以**, or **继续**, the referred
assistant proposal must have a stable local item identity and digest. If its
meaning or target cannot be determined safely, the result is
`needs_evidence`. An `existing_decision_adoption` disposition is available only
when the Recall domain already owns an explicit application/adoption receipt;
Task 7 does not infer it from text equality or a model relation. If the user
later restates the new rule explicitly in a native prompt, that new anchor can
support a normal Candidate.

When `confirmation_basis=adopted_decision_contract` and the selected receipt
Turns have recalled-Decision lineage, the result is `needs_evidence` unless an
explicit host-owned adoption receipt already exists. The model cannot clear
the lineage to make the Candidate eligible. A direct, explicit user direction
may still qualify in a recall-active Turn because the user's own instruction
remains authoritative, but its business meaning is still reviewed before
publication.

`needs_evidence`, adoption, and excluded dispositions are local diagnostic
results. They do not upload Candidate content and do not create a second Web
review queue in this slice.

## 7. Reconciliation and Central boundary

Candidate family reconciliation receives only locally eligible Candidates.
It compares business meaning as before, but provenance is host-owned:

- `same` preserves the observation provenance without creating a new revision;
- `refine` and `replace` bind the new revision to the triggering observation's
  unchanged provenance;
- `unrelated` creates a family from that observation's unchanged provenance;
- `ambiguous` uploads nothing; and
- the reconciliation model cannot return evidence IDs, a provenance kind, or
  a provenance digest.

A provenance-bearing observation may compare `same` with a legacy family
without creating a revision. `refine` or `replace` against a provenance-free
legacy family is forced to `ambiguous` and uploads nothing in this slice. Task
7 does not invent a mixed legacy/new lineage or add Registry migration UI.

Local revision integrity binds both Candidate content and provenance digest.
Central receives only a versioned provenance kind and digest alongside the
existing Candidate revision and evidence digest. It never receives local
receipt IDs, Session/Turn identities, Prompt content, excerpts, or reference
lineage. New-protocol uploads missing the required provenance kind/digest are
rejected.

The existing Review page remains the semantic gate: the reviewer accepts,
edits, or rejects Candidate content. The existing preview and exact
**确认发布** action remain the publication gate. Provenance prevents a recalled
Decision from qualifying itself; it does not replace business judgment.

## 8. Recall and internal-task isolation

Before their first structured Turn, disposable Capture and reconciliation
Threads are registered in the shared local host store with purpose `capture`
or `reconciliation`. Recall activation, Turn gates, application receipts, and
Decision injection are denied for both purposes even when inherited text
contains native-looking Skill or mention content.

Typed recalled Decision envelopes remain useful reference context. Their
Decision IDs and digests may enter local reference lineage, but their text,
markers, and receipts never enter the prompt-anchor allowlist.

This replaces the former Task 7 plan to recognize
`ZDECISION_DECISION_ENVELOPE` and related text markers as the safety boundary.
Markers remain useful for rendering and diagnostics only.

## 9. Privacy and retention

- Hook event receipts, manifests, Candidate provenance, dispositions, and
  reference lineage stay in the local Private Store.
- Prompt, transcript, PRD, source, diff, code, tool output, and evidence
  excerpts are not copied into ZDecision persistence.
- Central receives Candidate content only for `candidate_eligible`, plus the
  minimal provenance kind and digest.
- Local receipts follow the owning Capture and Session retention policy. They
  are not formal Decision data and never enter Git.
- Logs and acceptance evidence contain only sanitized IDs, digests, states,
  counts, and timestamps.

## 10. Compatibility

This is a new Capture protocol revision. It does not mutate or recompute
existing Capture IDs, Candidate IDs, family revisions, Reviews, publication
records, or formal Decisions.

- Published Decisions remain authoritative and unchanged. When recalled, they
  are always `recalled_decision` reference context.
- Completed legacy Candidate operations remain readable under their original
  bytes and explicit Review/publish rules; ZDecision does not invent
  provenance for them.
- In-flight legacy operations resume only under their frozen legacy protocol.
- New Capture requests use the provenance protocol and cannot fall back to
  `extractor-v4` after a validation failure.
- New and legacy observations are never silently merged merely to manufacture
  a provenance chain.

No Registry schema or formal Decision schema changes in this slice.

## 11. Acceptance matrix

Automated acceptance must prove:

1. recalled Decision or host-probe context with no qualifying prompt anchor
   produces zero eligible Candidates;
2. assistant, tool, code, Capture artifact, or compaction summary alone
   produces zero eligible Candidates;
3. an independently anchored user direction can produce one Candidate even
   when recalled reference context discusses the same topic;
4. identical text in a native prompt and recalled envelope is distinguished by
   host source type rather than bytes or keywords;
5. an unknown, duplicated, reordered, cross-Session, post-boundary, or forged
   receipt fails the complete attempt;
6. a model-authored `explicit_user_direction` without a valid receipt does not
   qualify;
7. Extraction cannot change the Inventory signal's evidence set;
8. reconciliation preserves provenance for `refine` and `replace` and cannot
   generate it;
9. `existing_decision_adoption`, `needs_evidence`, and excluded results upload
   no Candidate content;
10. retry and crash recovery reuse the same manifest and digests;
11. Capture and reconciliation Threads reject Recall activation and gates; and
12. a recalled rule plus only an unrelated **继续** anchor is classified
    `needs_evidence` in the fixed semantic test corpus, while the test records
    that this is a quality assertion rather than a host proof; and
13. no raw source or native IDs cross the Central boundary.

The real Desktop Host Gate additionally proves:

- one ordinary native Prompt produces one stable Hook ledger event and one
  usable anchor;
- a Capture fork retains enough association for Inventory to select only IDs
  from the frozen manifest;
- compact, resume, Fork, and retry do not mint or rebind anchors; and
- a Hook-created continuation such as a `Stop` continuation is either
  distinguishable from physical-user submission or is conservatively treated
  as non-authoritative.

## 12. Hard stop rule

Packet 3 stops before retrieval implementation if the real Codex Desktop host
cannot support the association above without relying on raw rollout formats,
transcript filenames, CWD guessing, recency, marker keywords, or model-authored
origin claims.

If `UserPromptSubmit` cannot distinguish a Hook-created continuation from a
physical-user submission, ZDecision must not label the receipt
`native_human_evidence`. The permitted fallback is the deliberately weaker
`hook_observed_user_prompt_anchor` plus the existing human Review/publish gate.
If even that prompt-event association is unstable across Capture, compaction,
and retry, Task 7 records `capture_evidence_provenance_unavailable` and Packet
3 stops. It does not ship a marker-only exclusion.

## 13. Mature patterns used

This design adapts the following patterns without importing their runtimes:

- [Codex Hooks](https://learn.chatgpt.com/docs/hooks#userpromptsubmit) for the
  pre-provider prompt-event boundary;
- [DeepTutor source inventory](https://github.com/HKUDS/DeepTutor/blob/37c3db6df7e886aee4f61c97ec5e618b8ab379e8/deeptutor/services/session/source_inventory.py)
  and [reference validation](https://github.com/HKUDS/DeepTutor/blob/37c3db6df7e886aee4f61c97ec5e618b8ab379e8/deeptutor/services/memory/consolidator/references.py)
  for host-issued source IDs, bounded allowlists, and out-of-pool rejection;
- [GitHub Copilot Memory](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/)
  for source-scoped memory and use-time citation validation; and
- [Attribute First, then Generate](https://aclanthology.org/2024.acl-long.182/)
  for selecting evidence before generating attributed content.

These systems validate source identity and reference reachability; none makes
a model-generated citation equivalent to semantic proof or publication
authority.
