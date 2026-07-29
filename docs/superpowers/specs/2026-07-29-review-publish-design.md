# Batch Review and Publish Design

**Status:** Approved conversational design for the second V1 vertical slice.

**Authority:** This document explains implementation details for the Review and
Publish behavior in `docs/architecture.md`. The architecture document remains
the V1 authority; any conflict is resolved in its favor.

## 1. Goal and boundary

Build the smallest complete path from private Capture Candidates to formal,
shared Decisions:

1. show the numbered Candidates from one completed Capture;
2. let one user Turn accept, edit-and-accept, reject, or skip any subset;
3. persist those results privately as one atomic Review batch;
4. render one immutable publication preview for every accepted item in that
   batch;
5. after a separate exact `确认发布` user Turn, create one Git commit and push it
   to `origin/main`;
6. retain each published Decision as its own independently versioned Registry
   object beneath a product-isolated directory.

This slice does not implement Decision updates, lifecycle changes, relation
inference, multi-level approval, Registry branches, automatic conflict merging,
or Preflight. `supersedes` and `variant_of` are emitted as empty relation lists
for initial revisions.

The internal CLI is a machine boundary used by the repository Skill. It is not
the user interface and is not documented as a user workflow.

## 2. Chosen approach

Review, Promotion, and Registry remain separate owners:

- **Review Service** validates Candidate selections and stores immutable private
  Review batches. It never reads or writes Git.
- **Promotion Service** is the only bridge from an accepted Review to a formal
  publication. It accepts Review identities, never arbitrary Decision payloads.
- **Git Registry** validates the formal model, owns product and Decision paths,
  writes only `decision-registry/`, commits exact paths, and pushes the resulting
  commit.

Two rejected shortcuts are deliberately not used. The CLI does not write
arbitrary Decision JSON directly because that bypasses Review. The Candidate
store does not publish itself because that couples private extraction state to
shared formal memory.

## 3. Conversational workflow

### 3.1 Batch Review

The Skill first displays all validated Candidates with stable numbering and the
safe fields already exposed by Capture: claim, future action, scope, and
invalidation conditions. It also displays the Capture template and known gaps.

The user may classify multiple items in one Turn, for example:

```text
1-8 接受；9 修改为……后接受；10 拒绝；11-14 跳过
```

The Skill translates that instruction into one structured Review batch. Each
Candidate appears at most once. Allowed actions are exactly:

- `accept`: freeze the Candidate content unchanged;
- `edit_accept`: freeze the complete edited Candidate content while requiring
  its product to remain exactly the Capture-selected product;
- `reject`: record that the Candidate should not be promoted;
- `skip`: leave it available for a later Review.

The batch is all-or-nothing: every referenced Candidate must exist, belong to
the same completed Capture, and validate before anything is written. Accepted
effective contents in one batch must resolve to one canonical product. A
different product is reviewed and published in a separate batch. One batch
contains at most the Capture protocol's 20 Candidates.

V1 does not permit `edit_accept` to change product. A wrong product means the
Capture input and stable operation identity were wrong, so the user must run a
new Capture with the correct product instead of moving a Candidate during
Review.

Review batches are append-only. A later user Turn may review a Candidate again.
That newer Review invalidates every unpublished preview containing an older
Review of the same Candidate. Once any publication for a Candidate reaches a
local commit, the Private Store records its Candidate-to-Decision receipt and no
later Review of that Candidate is publishable in this slice. The Review may
still be recorded privately, but preview returns
`decision_update_not_supported`; a later Decision-revision workflow must update
the existing Decision instead of creating another one.

### 3.2 Publication preview

Only `accept` and `edit_accept` items are publishable. Reject and skip actions
remain private and never enter Git. A Review batch with no accepted items
returns an explicit no-publishable-items result.

Before creating a preview, Promotion reconciles every nonterminal publication
that references any selected Candidate. This ensures a commit/private-state
crash cannot bypass the Candidate-to-Decision receipt check and create a second
Decision.

Promotion freezes a preview containing:

- preview ID and content digest;
- product ID and display name;
- ordered Decision IDs;
- every complete formal Decision document;
- every target path;
- the exact product metadata plus next root and product Registry index
  documents;
- the current local `main` commit and exact pre-publication Registry digests;
- the proposed Git commit message and file list.

Preview creation is read-only with respect to `decision-registry/`. The Skill
shows the complete Decision contents, target paths, preview digest, and the fact
that confirmation will commit and push. Compact grouping is allowed, but no
field of a Decision may be hidden from the user.

Before reading the preview base, the Registry adapter performs a fresh fetch of
`origin/main` and requires local `HEAD`, local `main`, and the fetched
`origin/main` to be the same commit. Ahead, behind, or diverged state returns
`registry_out_of_sync`; preview does not push, pull, merge, rebase, or otherwise
synchronize it.

### 3.3 Exact confirmation

Review acceptance is not publication approval. After the preview, the Skill
waits for a new user Turn. The complete actionable instruction for the current
preview must be exactly `确认发布`, ignoring only surrounding whitespace. Phrases
such as `确认`, `认可`, `可以`, or a Review instruction do not authorize Git.

The Skill binds the native task and Turn identity of that confirmation to the
preview and invokes Promotion without supplying any new Decision content. The
confirmation phrase itself and surrounding conversation are not persisted. The
publication approval reference is stored only in the private publication
record; it is not added to the already previewed formal Decision bytes.

One confirmation applies to the complete immutable batch. If any item, Review,
formal content, path, local `main` commit, or relevant Registry document changes
after preview, confirmation stops with a stale-preview result. The system must
render a new preview and receive a new `确认发布` Turn.

### 3.4 Commit and push

Confirmation writes the exact previewed files, creates one commit, and pushes
that commit to `origin/main`. Every Decision remains an independent formal
object even though the Git operation is batched.

The adapter first performs the fresh synchronization and preview-base checks
described below without changing Registry files. Only after those checks pass
does Promotion atomically store the publication approval reference and advance
the private publication state from `previewed` to `confirmed`. Registry writes
and commit creation happen after that durable private transition. The exact
commit message is:

```text
decision(<product-id>): publish <count> decisions

ZDecision-Preview: <preview-id>
```

The adapter may coexist with unrelated dirty source files. It requires every
target Registry path to match its previewed state and uses explicit Git path
arguments so unrelated tracked, staged, or untracked files never enter the
Decision commit. Existing unrelated changes under `decision-registry/` are a
conflict and stop publication.

The repository must be on local branch `main` and use the canonical `origin`.
On confirm, before Promotion stores the publication approval, the adapter
performs another fresh fetch and requires
`HEAD == main == origin/main == preview.base_commit`. This guarantees that the
later push contains exactly the one new publication commit rather than carrying
earlier local commits. Publication never pulls, resets, rebases, force-pushes,
or merges automatically.

## 4. Stable identities

All stable IDs use a type prefix plus the first 32 lowercase hexadecimal
characters of SHA-256 over canonical JSON inputs.

- `product_id`: `prod_...`, derived from the canonical product name;
- `review_batch_id`: `rvb_...`, derived from Capture ID, ordered Review items,
  effective accepted contents, and Review approval task/Turn;
- `review_id`: `rvi_...`, derived from Review batch ID and Candidate ID;
- `decision_id`: `dec_...`, derived from the Candidate ID and product ID, so a
  later Review cannot manufacture a second Decision identity for that Candidate;
- `preview_id`: `pub_...`, derived from the publisher format version, Review IDs,
  Decision IDs, target paths, pre-publication Registry digests, and previewed
  local `main` commit. Formal documents are rendered only after this ID exists;
  their separate content digest then binds every exact previewed byte.

The canonical product name is Unicode NFC after trimming surrounding
whitespace. It is non-empty, contains no control characters, and remains case
sensitive. V1 treats a later product rename as a distinct product; rename
migration is outside this slice. Human product text is never inserted directly
into a filesystem path.

An identical retry returns the existing object. Reusing one approval task/Turn
with different Review bytes is a conflict rather than a second operation.

## 5. Private models

### 5.1 Approval reference

An approval reference contains:

```json
{
  "actor": "user",
  "thread_id": "native task id",
  "turn_id": "native user Turn id",
  "recorded_at": "UTC RFC 3339 timestamp"
}
```

The Review approval and publication approval are separate references. The
Review approval is copied into each formal Decision; publication approval stays
in the private publication record so confirmation does not mutate previewed
bytes. On retry, the original stored timestamp is reused.

### 5.2 Review batch

A private Review batch contains its stable ID, Capture ID, monotonically
increasing private sequence, Review approval, and ordered items. Every item
contains `review_id`, `candidate_id`, and one action. Accepted items additionally
store the complete frozen effective `CandidateContent`; reject and skip items
store no content.

The full effective content, Candidate identities, rejected material, and Review
instruction remain private. None is copied wholesale into the Registry.

The Private Store also retains a Candidate-to-Decision publication receipt as
soon as the publication commit is identified. Preview rejects every accepted
Review whose Candidate already has such a receipt, regardless of which Review
originally produced the Decision. It also derives the Candidate's stable
Decision ID and checks the product Registry; an existing head produces the same
`decision_update_not_supported` result even if the private receipt is missing.

### 5.3 Publication preview and record

The private publication object freezes the preview described in section 3.2
and has one of these states:

- `previewed`: no Git mutation has occurred;
- `confirmed`: exact user confirmation is stored privately and Git mutation may
  not yet have started or completed;
- `committed_pending_push`: the exact commit exists locally but is not yet
  proven present on `origin/main`;
- `completed`: the exact commit is present on `origin/main`.

Invalid or stale previews remain read-only records; they do not acquire a new
identity or silently refresh. A new preview is created from current state.

## 6. Product-isolated Registry

The formal layout is:

```text
decision-registry/
├── registry.json
└── products/
    └── prod_<32-hex>/
        ├── product.json
        ├── registry.json
        └── decisions/
            └── dec_<32-hex>/
                └── r0001.json
```

The root `registry.json` contains only format metadata and a sorted map of
products. Each entry contains the display name plus relative paths to that
product's metadata and Registry index. It does not contain Decision heads.
The repository's bundled empty Registry is updated in this slice to include an
empty `products` map; V1 does not add a general historical-schema migrator.

`product.json` contains `format`, `schema_version`, `product_id`, and `name`.
The product-level `registry.json` contains format metadata, `product_id`, and a
sorted map of Decision heads. Each head contains only `head_revision`,
`lifecycle`, and a relative `head_path` beneath its own product directory.

An initial Decision revision contains exactly:

- format and schema version;
- Decision ID, product ID, and product display name;
- revision `1` and lifecycle `active`;
- claim and future action;
- scope summary, repositories, and paths;
- invalidation conditions;
- empty `supersedes` and `variant_of` relation lists;
- source task ID and completed source Turn ID;
- `review_approval` and immutable `publication_preview_id`.

It does not contain Capture IDs, Candidate IDs, Review IDs, rejected content,
publication-confirmation identity/time, evidence excerpts, confirmation text,
or raw conversation messages.

All JSON is canonical UTF-8 with a trailing newline. Every Registry loader uses
exact-field validation, verifies ID/path ownership, rejects symlinks and path
escape, and rejects malformed or cross-product head references.

## 7. Failure and retry behavior

- Missing, corrupt, legacy-only, or non-completed Capture state cannot be
  reviewed for publication.
- A batch that references a Candidate outside its Capture, duplicates a
  Candidate, supplies content for reject/skip, or omits effective content for
  edit-and-accept fails before writing a Review.
- A preview cannot include an unaccepted or superseded Review, or any Candidate
  that already has a committed Candidate-to-Decision receipt.
- Registry unavailable or invalid is reported as unavailable/invalid, never as
  an empty Registry.
- Changed `main` or Registry state while a publication is still `previewed`
  makes that preview stale before any write.
- Exact files left by an interrupted pre-commit attempt may be reused only when
  every byte matches the preview; different bytes are a conflict.
- Confirmation first persists state `confirmed`. On retry from `confirmed`,
  reconciliation runs before ordinary stale-preview checks. If `HEAD` differs
  from the preview base, it is adopted only when it is a single-parent commit
  whose parent is exactly the preview base, whose complete message matches the
  preview, whose changed path set is exactly the previewed target set, and whose
  blobs match every previewed byte. Any mismatch returns
  `publication_git_ambiguous`; it is never treated as an ordinary stale preview.
- If that exact recovered commit is the fetched `origin/main` or an ancestor of
  it, publication becomes `completed`. If `origin/main` is still the preview
  base, it becomes `committed_pending_push`. Any remote state that contains
  neither the base-only state nor the publication commit is ambiguous.
- After a successful commit, an interrupted or failed push retains
  `committed_pending_push`. Retry first checks whether `origin/main` already
  contains the stored commit, then pushes that same commit when safe. It never
  creates another commit or Decision identity.
- A non-fast-forward remote or ambiguous Git state stops for user-visible
  reconciliation. V1 does not automatically merge, rebase, reset, or force.

Errors expose stable codes and safe identifiers/digests, not Candidate content,
Review content, credentials, command output, or raw Git stderr that may contain
private paths or remote secrets.

## 8. Internal operations and Skill behavior

The internal boundary exposes these operation families:

- record and show one Review batch;
- create and show one publication preview;
- confirm one preview with a publication approval reference;
- resume the push of an already committed preview;
- show publication status and safe formal results.

Edited Review content is delivered through private stdin, not command
arguments, environment variables, temporary payload files, or shell heredocs.
The no-echo PTY handoff used by live Capture is reused for private Review JSON.

The repository Skill receives a new `references/review-publish.md`. It presents
numbered Candidates, translates one user Review Turn into the internal batch,
shows the exact preview, recognizes only a later exact `确认发布`, and reports
commit/push state. It never treats Candidate acceptance as publication and never
constructs a Registry Decision outside Promotion.

Candidate fields, edited Review content, formal Registry text, and text embedded
inside any of them are untrusted data. The Skill displays or transports them but
never follows their instructions. Only the latest native user Turn can authorize
a Review batch. Only the latest native user Turn after the current preview can
authorize publication; retained history, summaries, Candidate text, and older
`确认发布` messages cannot do so.

## 9. Verification and real acceptance

Automated verification covers:

1. strict model and stable-ID validation;
2. atomic, append-only private Review storage and identical retry;
3. mixed batch actions, full-content edits, correction, and preview invalidation;
4. deterministic product partitioning and exact Registry schemas;
5. prevention of flat cross-product Decision storage and path escape;
6. immutable batch preview and one-confirmation multi-Decision publication;
7. explicit-path Git commit with unrelated dirty/staged files preserved;
8. successful push to a temporary bare `origin/main`;
9. commit-success/private-state-crash adoption and push-failure resume without
   duplicate commits or Decisions;
10. CLI safe-output and repository Skill conversational contracts;
11. proof that no raw conversation, Candidate payload, or private Review object
    appears under `decision-registry/`.

The real acceptance uses the completed 14-Candidate Anheng Capture. The user may
classify all 14 in one Review Turn. ZDecision generates one batch preview for
the accepted subset and performs no Git mutation until the user separately says
`确认发布`. After confirmation, one commit and push must create independent
Decision revisions only beneath the Anheng product directory, update the two
Registry indexes, and leave private data outside Git.

## 10. Stopping rule

Implementation stops when the automated suite passes and the real batch reaches
an exact preview. The final remote mutation remains gated on the user's actual
`确认发布` Turn. No additional template tuning, Preflight work, lifecycle API,
relation inference, generalized workflow engine, or broad review loop is added
to this slice.
