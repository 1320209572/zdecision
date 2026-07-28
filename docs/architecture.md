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

V1 maps app-server operations to product behavior explicitly:

| App-server operation | ZDecision use |
|---|---|
| `thread/read` | Read source-task metadata, completed Turns, and user confirmation visible in the controlling task. |
| `thread/fork` | Create an isolated Capture task from the selected completed source boundary. |
| `turn/start` | Run extraction in the Capture fork and start the first Turn of a newly created task. |
| `thread/start` | Create a genuinely new task after Preflight has produced approved context. |
| `thread/resume` | Continue the same development goal without creating another ZDecision task. |
| `turn/steer` | Correct an executing Turn while preserving the same goal and task identity. |

The app-server adapter owns protocol details and capability checks. Domain
components receive typed results; they do not construct JSON-RPC requests or
infer business decisions from protocol events.

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

### 4.4 Scenario contracts

- **Compress an existing task:** the user supplies its task ID; Capture reads a
  completed boundary and forks from it. The source task may continue and is not
  steered merely to produce a summary.
- **Correct a Candidate:** the user edits or rejects private Candidate content;
  no Git history is created until a later exact publication confirmation.
- **Hand work to a new developer or goal:** Preflight selects applicable formal
  Decisions and starts a new task with only that bounded context.
- **Continue the same goal:** use `thread/resume`; do not run Preflight or create
  another task simply because time or developer identity changed.
- **Correct an executing task:** use `turn/steer`; steering is not a new durable
  Decision unless the result is later captured and reviewed.
- **No usable Registry context:** distinguish empty, no-applicable, stale, and
  unavailable results, then show the user the allowed next action.

## 5. Component boundaries

V1 keeps the complete architecture, but each component has one narrow
responsibility.

### 5.1 Codex routing layer

`AGENTS.md` and the repository ZDecision Skill recognize user intent, present
review/confirmation text, and invoke tested internal operations. They do not
contain decision logic or persistence logic. The Skill is the conversational
workflow; internal commands are its stable machine boundary.

### 5.2 App-server Gateway

The Gateway is the only component that speaks Codex app-server protocol. It
reads source tasks, forks Capture tasks, runs Turns, observes confirmation, and
creates or continues tasks. It returns stable ZDecision records such as a
source checkpoint or created-task result.

### 5.3 Capture Service

Capture selects one completed source boundary, asks the Gateway for an isolated
extraction run, validates the structured result, and stores zero or more
Candidates. It never writes formal Decisions.

### 5.4 Private Store

The Private Store persists Capture checkpoints, Candidate drafts, user reviews,
publication previews, Context Packs, and task-usage records in user-local data
outside Git. It supports ordinary replay of user-visible writes so a command
retry does not create an obvious duplicate. It is not shared project memory.

### 5.5 Promotion Service

Promotion is the only application boundary that can convert an accepted
Candidate into a Registry mutation. It reloads the reviewed Candidate, renders
the exact formal Decision, binds explicit user confirmation to that preview,
and then asks the Registry to publish. A caller cannot bypass review by sending
an arbitrary Decision payload directly to Git.

### 5.6 Decision Registry

The Registry owns formal Decision identity, immutable revisions, lifecycle,
relations, and minimal provenance. Its V1 adapter reads and writes only
`decision-registry/` in the canonical repository on `main`. Git is the storage
adapter and audit history; commit hashes are not Decision identity.

### 5.7 Applicability Engine

Applicability compares active formal Decisions with one target workspace and
user goal. It produces `matched`, `conflict`, `not-applicable`, or `unknown` as
an ephemeral result. It cannot change Decision lifecycle.

### 5.8 Context Assembler

The assembler converts applicability results into a bounded Context Pack. It
orders complete Decision items, records excluded items, and never truncates a
Decision in the middle. It does not fetch conversations or mutate the Registry.

### 5.9 Preflight and Task Gateway

Preflight inspects the target checkout, queries the Registry, runs
Applicability, builds the available Context Pack choices, and obtains the
user's explicit choice when data is stale or unavailable. The Task Gateway then
uses app-server `thread/start` and `turn/start` to create exactly the requested
new task and records which Decision revisions were supplied.

The intended package boundaries are:

```text
src/zdecision/
  app_server/      protocol adapter and Task Gateway
  capture/         Capture and Candidate review
  private_store/   user-local state
  registry/        formal model, Promotion, and Git adapter
  preflight/       workspace applicability and Context Pack assembly
```

No component imports a deleted legacy package, and there is no central
Coordinator above these services.

## 6. State ownership and lifecycle

| Object | Owner and location | Mutability and lifetime |
|---|---|---|
| Source Checkpoint | Capture / Private Store | Immutable reference to one completed source Turn. |
| Candidate | Capture / Private Store | Private and editable during review; never formal project memory. |
| Candidate Review | Review / Private Store | Append-only record of the user's accept, edit, reject, or skip action. |
| Decision Identity | Decision Registry | Stable for the life of the Decision. |
| Decision Revision | Decision Registry | Immutable once published; later changes create a new revision. |
| Decision State | Decision Registry | Lifecycle may change only through an explicit confirmed operation. |
| Applicability Result | Applicability Engine | Ephemeral result for one workspace and task; never authoritative lifecycle. |
| Context Pack | Preflight / Private Store | Immutable input prepared for one new-task choice. |
| Task Usage | Private Store | Lightweight record of the Context Pack and revisions supplied to a created task. |

Candidate lifecycle is deliberately separate from Decision lifecycle:

```text
proposed Candidate
  -> accepted / edited-and-accepted
  -> rejected / skipped

accepted Candidate
  -> explicit publication preview
  -> user confirmation
  -> published Decision revision
```

An accepted Candidate is still private until the final confirmation boundary.
A published Decision begins as `active`; an explicit later operation may add a
new revision, supersede it, or retire it. Historical revisions are never
rewritten. A checkout conflict or an invalidation condition may produce a
warning, but cannot silently change formal state.

Minimal provenance links a Decision back to the source task/checkpoint and
approval without copying source messages. `supersedes` and `variant_of` are
formal relationships only when the user reviewed them as part of publication.

## 7. Storage boundary

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

## 8. Failure and retry behavior that V1 must preserve

- A source task with an active head uses a completed Turn boundary.
- Missing source data stops Capture; it does not invent evidence.
- Registry unavailable is reported as unavailable, never as empty.
- No applicable Decision is a valid, distinct result.
- Candidates cannot publish themselves.
- Applicability may warn about conflict or uncertainty; it cannot automatically
  retire or supersede a formal Decision.
- Git writes are restricted to `decision-registry/` and never include unrelated
  workspace changes.

V1 uses stable operation identity at the three user-visible write boundaries:
Capture, publication, and new-task creation. Retrying the same completed
operation returns or reconciles its existing result rather than intentionally
creating another Candidate set, Decision revision, or task. Ambiguous external
results stop for reconciliation; they are not a reason to invent a replacement
business result.

This is ordinary single-user robustness, not a generalized distributed worker
protocol. The design adds stronger coordination only after observed concurrent
usage requires it.

## 9. V1 completion boundary

V1 is not a disposable validation prototype. The three slices below are an
implementation order for one complete product path. V1 is complete only when a
user can, from a Codex conversation:

1. identify an existing task and receive validated Candidates or an explicit
   no-decision result;
2. review exact Candidate content and publish one confirmed formal Decision to
   `decision-registry/` on `main`;
3. start a genuinely new task whose first Turn receives the applicable bounded
   Decision context;
4. continue or steer an unchanged goal through native Codex operations without
   ZDecision creating an unnecessary task;
5. distinguish empty, no-applicable, stale, and unavailable Registry outcomes;
6. inspect which formal revisions were supplied to the new task; and
7. verify that raw source content and private Candidate/review data never
   entered Git.

The repository Skill, internal operation boundary, services, persistence, Git
adapter, and app-server integration needed for those scenarios are all part of
V1 even though they are delivered incrementally.

The following remain explicitly outside V1:

- an independent Registry repository or Registry branch;
- a background coordinator, task graph, or worker scheduler;
- compatibility with the deleted legacy architecture or data contracts;
- distributed locking and exhaustive multi-process crash recovery;
- multi-level approval or organization policy engines;
- automatic lifecycle/relation inference;
- CLI-first product UX;
- snapshot-history migration machinery for a hypothetical future backend.

These may be reconsidered from observed usage, not pre-built speculatively.

## 10. Implementation order

Build three end-to-end slices:

1. **Capture:** read a source task through app-server and produce private
   Candidates or an explicit no-decision result.
2. **Publish:** review one Candidate and write one confirmed Decision beneath
   `decision-registry/` on `main`.
3. **Use:** query relevant Decisions, build a bounded Context Pack, and start one
   new Codex task with it.

Each slice must be demonstrable from a Codex conversation before the next slice
adds abstractions. A slice is a review checkpoint, not a reduced edition of the
architecture; the final V1 acceptance gate covers the complete sequence across
all three.

## 11. Reuse rule

The previous implementation is not a dependency or compatibility target. If a
generic behavior is needed later—canonical JSON, safe Git argument handling, or
app-server pagination—it is reimplemented and tested under the new component
that owns it. No legacy module, test fixture, Skill, or schema is retained merely
as reference.
