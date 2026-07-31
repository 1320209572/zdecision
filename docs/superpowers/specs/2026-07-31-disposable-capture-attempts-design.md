# ZDecision Disposable Capture Attempts Design

**Status:** Approved for implementation planning.

**Scope:** Correct the app-server recovery contract in Packet 1 of the
page-authorized Candidate refresh loop.

**Amends:** `2026-07-30-on-demand-candidate-refresh-design.md`.

## 1. Problem

The first Packet 1 implementation treated a native Codex Thread or Turn as a
recoverable business object. Before `thread/fork` or `turn/start`, it persisted
a stable tag; after an unknown transport result, it expected to find the
created object through `threadSource` or `clientUserMessageId`.

The real Codex app-server acceptance run disproved that assumption. Those
fields do not provide a documented, reliable exactly-once creation or lookup
contract for an unknown ephemeral object. A fake that remembers those tags
therefore cannot establish production recovery.

ZDecision does not need exactly-once model execution. It needs exactly-once
Candidate effects.

## 2. Decision

Packet 1 separates durable business operations from disposable native
execution:

```text
frozen source boundary
        |
        v
durable CaptureOperation
        |
        +-- ExecutionAttempt generation 1 -- abandoned
        |
        +-- ExecutionAttempt generation 2 -- validated
        |
        v
single CAS result commit
        |
        v
Candidate reconciliation and immutable outbox batch
```

A `CaptureOperation` is authoritative. A Codex Thread or Turn is a read-only
compute attempt and may be lost, duplicated, deleted, or abandoned without
changing the logical operation.

Model execution is at-least-once. A fenced local commit makes its Candidate
effects exactly-once.

## 3. Frozen operation input

One source operation freezes:

```text
request_id
repository_id
source_key
session_id
lineage
previous_handled_turn_id
upper_completed_turn_id
source_fingerprint
template snapshot and rendered prompts
model profile
capture protocol revision
```

The source fingerprint is a local canonical digest of the observed frozen
boundary metadata. The source Thread ID plus completed upper Turn ID remains
the official content boundary. Later activity in the same Session belongs to
the next page request and cannot change this operation.

The normal source route is the official app-server `thread/read` followed by
`thread/fork(lastTurnId)`. Packet 1 does not parse, copy, hash, or persist the
Hook's `transcript_path` JSONL. Its format is not a stable Hook interface.
If the official source boundary is unavailable or no longer matches the
frozen identifiers, the operation fails closed as
`source_boundary_unavailable`.

## 4. CaptureOperation

The durable operation owns:

```text
operation_id
record_version: extractor-v3
frozen input and input digest
status: open | committed | failed_terminal
active_generation
committed_generation
committed_result_digest
validated Inventory
validated Candidate Observations
```

The deterministic operation identity includes every frozen input that can
change extraction meaning. Reopening the same identity returns the same
operation. A conflicting input digest is corruption, not a new attempt.

Only the operation may authorize Candidate Observations. Native Thread and Turn
IDs never determine Candidate IDs or whether a source checkpoint is handled.
Existing extractor-v2 Capture records remain readable under their current
contract; they are not rewritten or silently upgraded into this on-demand
operation model.

`failed_terminal` is reserved for immutable-input, authorization, unsupported
capability, or durable-state failures that another model attempt cannot repair.
A timeout, refusal, transport failure, or invalid structured model result
abandons only that attempt and follows the request's bounded retry policy.

## 5. ExecutionAttempt

An attempt is identified by `(operation_id, generation)` and records bounded
local execution state:

```text
state: prepared | creating_thread | running | validated
       | accepted | superseded | abandoned
started_at and finished_at
known native Thread and Turn IDs, when returned
failure category
validated structured result and canonical digest, when available
```

Before any native mutation, the Agent commits a new monotonically increasing
generation and makes it active. One attempt runs the complete two-stage
protocol in one fresh persisted, read-only fork:

```text
fork the frozen source through upper_completed_turn_id
  -> Inventory Turn
  -> validate the complete Inventory
  -> Extraction Turn in the same retained context
  -> validate the complete Candidate Observation set
  -> CAS commit the operation result
```

Inventory and Extraction stay in the same attempt because Extraction may rely
on both the retained source context and the validated Inventory Turn. If a
native result becomes unknown at either stage, the whole attempt is abandoned;
the next retry creates a fresh fork and reruns both stages.

Persisting an attempt Thread is a transport-level recovery aid, not business
identity. A known Thread ID may be read for diagnostics after restart and is
archived when its attempt becomes terminal. If the fork response is lost
before the ID is known, ZDecision accepts that the blank fork may be an
unarchivable orphan; it cannot contain Capture Turns because ZDecision never
received the ID needed to start one.

`threadSource` and `clientUserMessageId` may remain bounded diagnostics when
the installed Codex version accepts them. Correctness must be unchanged when
they are absent, ignored, duplicated, or not returned.

## 6. Fencing and result commit

An attempt may commit only when:

1. its generation is still the operation's `active_generation`;
2. both complete stage outputs passed the existing schema, size, count,
   privacy, and template-boundary checks; and
3. the operation is still `open`.

Before attempting that CAS, the attempt atomically stores its complete
validated structured result and digest. The operation commit then atomically
records the selected generation, validated Inventory, Candidate Observations,
and canonical result digest. A crash between those writes resumes from the
durable validated attempt and does not invoke Codex again.

- Replaying the selected generation with the same digest returns the existing
  receipt.
- A result from an older or abandoned generation is discarded, even if it
  arrives late and is otherwise valid.
- A second result with a different digest cannot mutate a committed operation.
- A crash before the commit leaves the operation open and retryable.
- A crash after the commit resumes from the committed result without invoking
  Codex again.

Known native tasks are archived after their attempt becomes terminal. Archive
failure retries archive only; it never reopens a committed operation or reruns
model work.

## 7. Reconciliation and Candidate CAS

After all selected source operations reach a terminal result, the Agent freezes
their ordered Observation set and digest. Reconciliation model calls use the
same disposable-attempt and generation-fencing rule.

The winning reconciliation commit atomically records, for one `request_id`:

- the frozen Observation-set digest;
- the validated reconciliation result;
- Candidate family revisions and heads;
- the immutable upload batch, or the explicit zero-Candidate receipt; and
- the canonical commit digest.

An exact replay returns the existing commit. A different digest for the same
request is a terminal conflict. A late reconciliation attempt cannot change
family state or the outbox.

Central upload remains idempotent. A central outage resends the exact committed
batch; it never reruns Capture or reconciliation. Session handled checkpoints
advance only after the central service acknowledges that exact batch or
zero-Candidate receipt.

## 8. Failure rules

| Failure point | Required behavior |
|---|---|
| Explicit app-server rejection before native creation | Mark the attempt abandoned and retry under existing request policy. |
| Unknown fork or Turn result | Abandon the generation; start a new whole attempt from the same frozen boundary. |
| Agent crash before operation CAS | Commit an already validated active attempt; otherwise start a new generation. |
| Agent crash after operation CAS | Use the committed structured result; make no model call. |
| Late result from an abandoned generation | Fence it out and retain only bounded diagnostics. |
| Frozen source missing, changed, or unreadable | Fail terminal; upload no Candidate and do not advance the handled checkpoint. |
| Invalid or oversized model output | Abandon the attempt under the bounded retry policy; never persist it as a Candidate result. |
| Central outage after Candidate CAS | Replay the exact outbox batch until acknowledged. |
| Conflicting committed digest or corrupt local state | Fail terminal and require explicit repair. |

Failures never block ordinary Codex development.

## 9. Privacy

Raw Sessions, Prompts, source code, diffs, tool output, and complete app-server
Thread data stay outside central storage and Git. Packet 1 persists locally
only:

- frozen source identifiers and digests;
- exact system-owned template prompts and model profile;
- validated structured Inventory and Candidate artifacts;
- bounded attempt diagnostics; and
- Candidate commit and upload receipts.

The DeepTutor rollout parser remains architectural evidence that local
replayable input is practical, not code or a persistence contract to transplant
into Packet 1.

## 10. Acceptance

The corrected Gate C must prove:

1. An unknown fork result starts a second disposable attempt; it is not adopted
   through `threadSource`.
2. An unknown Inventory or Extraction result abandons the whole fork and
   reruns Inventory then Extraction in a fresh fork.
3. Deleting the first attempt's native task does not prevent completion.
4. A late valid result from the abandoned generation cannot commit.
5. Two completed attempts can produce different valid model output, but only
   the active generation can create Candidate effects.
6. A crash immediately before and after CaptureOperation CAS produces one
   committed result.
7. Reconciliation, family-head updates, and the outbox produce one logical
   Candidate commit across every crash point.
8. Zero Candidates commits once and ends as
   `succeeded_no_candidates`.
9. Later Session Turns are excluded from the frozen request and remain
   available to the next click.
10. Missing or changed source boundaries fail closed without advancing handled
    checkpoints.
11. Correctness is identical when `threadSource` and
    `clientUserMessageId` are omitted.
12. A supplied `transcript_path` is never opened, copied, hashed, persisted, or
    uploaded.

The real acceptance run must exercise at least one unknown native result
against the installed Codex app-server. Fake transports may verify fencing and
crash points, but cannot claim native-object recovery.

## 11. Implementation boundary

Keep the current Task 9 removal of zero-touch eligibility, automatic Capture,
manual Capture MCP tools, and the old Gate 3 runner.

Replace only:

- stable-tag native-object discovery;
- one-row-per-stage native attempt state;
- permanent binding of a Capture operation to its first fork;
- tests that require one native creation after an unknown result; and
- the corresponding recovery text in the active design and implementation
  plan.

Do not add JSONL import, Web Review/publication, recall, OIDC, multi-device
coordination, non-code Capture, or a generalized workflow engine in this
correction.
