# ZDecision V1 architecture

## 1. Goal

ZDecision preserves decisions that should influence future development tasks
without turning an entire Codex conversation into permanent project memory.

The system must support three real scenarios:

1. A user identifies an existing Codex task and asks ZDecision to extract the
   decisions that were actually confirmed there.
2. The user reviews those candidates and publishes only the decisions worth
   sharing.
3. A later developer or new Codex task receives the relevant decisions without
   receiving the source conversation.

## 2. User experience

The user clones this repository, opens it in Codex App, and speaks naturally.
Repository instructions route the request; users are not expected to run a
collection of CLI commands themselves.

Examples:

- “压缩任务 `<task-id>` 的决策。”
- “这条不是正式决策，删掉；剩下两条发布。”
- “读取安恒已有决策，然后为这个新目标创建任务。”

Internal commands may exist as a tested tool boundary, but they are an
implementation detail used by Codex.

## 3. Control plane

Codex App and Codex app-server remain the conversation and execution plane.
ZDecision uses the app-server capabilities for task reading, forking, starting,
resuming, and steering instead of building another conversation runtime.

The routing rule is simple:

- Same development goal: use native resume/steer.
- Correcting an executing task: use native steer.
- New goal or new-developer handoff: create a new task after Preflight.
- Extracting durable decisions: run Capture against a completed source-task
  boundary.

ZDecision is not a coordinator, scheduler, or replacement for Codex tasks.

## 4. V1 flow

```text
existing Codex task
        |
        | read a completed boundary and fork for extraction
        v
private Candidate decisions
        |
        | explicit user review and confirmation
        v
decision-registry/ on main
        |
        | query by product and current task scope
        v
bounded Context Pack
        |
        | app-server thread/start + turn/start
        v
new Codex task
```

### 4.1 Capture

Capture identifies one completed Turn in the source task and treats it as a
stable boundary. Extraction runs in a fork or dedicated turn so the source task
does not need to be rewritten or blocked.

Capture produces zero or more Candidates. A Candidate contains a concise claim,
the future action it implies, its product/scope, invalidation conditions, and
minimal source references. Raw conversation text remains private.

Zero Candidates is a valid result.

### 4.2 Review and publish

Candidates are private and editable. The user may accept, narrow, edit, reject,
or skip each Candidate.

Accepted Candidate does not mean published Decision. ZDecision shows the exact
formal content to be written and requires explicit confirmation before changing
Git.

The formal Decision model stays small:

- stable decision ID and product;
- revision;
- claim and future action;
- scope and invalidation conditions;
- lifecycle (`active`, `superseded`, or `retired`);
- optional `supersedes` / `variant_of` references;
- minimal provenance and approval identity/time.

### 4.3 Preflight and new task

For a genuinely new task, Preflight inspects the target product checkout,
queries active Decisions, and classifies each as matched, conflicting,
not-applicable, or uncertain.

It builds a bounded Context Pack containing complete Decision items. It never
silently truncates a Decision. The user sees stale or unavailable Registry
state explicitly before choosing whether to continue without fresh context.

The approved Context Pack is supplied to the first Turn of the new Codex task.
The new task receives formal decisions, not the source conversation or private
Candidates.

## 5. Storage boundary

V1 deliberately uses one Git repository and one branch:

```text
zdecision/
  AGENTS.md
  src/zdecision/
  decision-registry/
  docs/architecture.md
```

- Canonical remote: `https://github.com/1320209572/zdecision.git`
- Branch: `main`
- Formal decision subtree: `decision-registry/`

There is no independent Registry branch in V1. The subtree boundary must remain
explicit in code so a later version can move storage without changing Decision
identity.

Only formal reviewed state belongs in `decision-registry/`. The following are
forbidden there:

- raw Codex task/session content;
- Candidate or rejected material;
- evidence excerpts;
- private review databases;
- workspace snapshots or dirty files;
- credentials and secrets.

Private state lives in user-local application data outside this repository.

## 6. Failure behavior that V1 must preserve

- A source task with an active head uses a completed Turn boundary.
- Missing source data stops Capture; it does not invent evidence.
- Registry unavailable is reported as unavailable, never as empty.
- No applicable Decision is a valid, distinct result.
- Candidates cannot publish themselves.
- Applicability may warn about conflict or uncertainty; it cannot automatically
  retire or supersede a formal Decision.
- Git writes are restricted to `decision-registry/` and never include unrelated
  workspace changes.

V1 needs ordinary idempotency at user-visible write boundaries, but it does not
need a generalized distributed recovery protocol before the first vertical
slice works.

## 7. Explicitly outside V1

- an independent Registry repository or Registry branch;
- a background coordinator, task graph, or worker scheduler;
- compatibility with the deleted legacy architecture or data contracts;
- distributed locking and exhaustive multi-process crash recovery;
- multi-level approval or organization policy engines;
- automatic lifecycle/relation inference;
- CLI-first product UX;
- snapshot-history migration machinery for a hypothetical future backend.

These may be reconsidered from observed usage, not pre-built speculatively.

## 8. Implementation order

Build three end-to-end slices:

1. **Capture:** read a source task through app-server and produce private
   Candidates or an explicit no-decision result.
2. **Publish:** review one Candidate and write one confirmed Decision beneath
   `decision-registry/` on `main`.
3. **Use:** query relevant Decisions, build a bounded Context Pack, and start one
   new Codex task with it.

Each slice must be demonstrable from a Codex conversation before the next slice
adds abstractions.

## 9. Reuse rule

The previous implementation is not a dependency or compatibility target. If a
generic behavior is needed later—canonical JSON, safe Git argument handling, or
app-server pagination—it is reimplemented and tested under the new component
that owns it. No legacy module, test fixture, Skill, or schema is retained merely
as reference.
