# Task 5 implementation report

## Status

Implemented immutable exact publication Preview creation and read status for
accepted central Review items, plus the independent browser confirmation page.
Task 5 creates no central publication row, Git commit, push, resume operation,
receipt, or publish mutation.

## Implementation

- Added `CentralPreviewService.create/get/check_publishability`. It loads one
  browser-owned immutable Review batch, selects only `accept` and
  `edit_accept`, proves each item is still the latest Review and current
  Candidate head, rejects already published families, and converts the frozen
  effective content to existing V1 `DecisionSeed` values.
- Derived organization, actor, product identity, Candidate/Decision IDs,
  central opaque source coordinates, Registry paths, formal bytes, preview ID,
  digests, and commit message on the server. Rejected/skipped content and
  private notes never enter the artifact or response.
- Reused the historical `RegistryCatalog.inspect/render`,
  `PublicationRecord`, `PublicationFile`, and immutable SQLite Preview table.
  Display documents and changed files freeze sorted canonical UTF-8 bytes and
  SHA-256 values; Preview and action replay insert in one `BEGIN IMMEDIATE`
  transaction.
- Hardened exact-base creation beyond a worktree cleanliness check: Catalog
  planning occurs against a bounded temporary archive of the proven commit,
  read with `git --no-replace-objects`. An `assume-unchanged` worktree edit or
  replacement object can neither supply base bytes nor enter the frozen
  artifact. The exact base is re-proved inside the freeze transaction.
- Recomputed read publishability without replacing the artifact. A newer
  Review, Candidate head, publication receipt, known changed main base, plan,
  digest, path, or rendered byte makes it `stale`; an unreachable or otherwise
  unprovable Registry makes it `registry_unavailable`. The original frozen
  record remains readable in either state.
- Added strict Preview POST/GET routes and sanitized 404/409/503 mappings. The
  response includes full Decision values, exact canonical JSON, all display
  and changed files, paths/digests, base commit/Registry digests, content and
  Preview identities, commit message, publishability, and optional historical
  publication identity.
- Added Candidate Review composition that follows a preview-eligible immutable
  Review submission with one explicit Preview action, then navigates to the
  independent Preview route. A Preview failure retains the immutable Review
  batch and exact action identity in browser storage, exposes an explicit retry,
  and never resubmits the Review or invents a publication.
- Added a dense manifest-style Preview page with complete Decision field
  panels, open/expandable exact JSON `<pre>` blocks, root/product Registry
  documents, target paths, base/digests, changed files, commit message, and a
  link back to the product Candidate page. React renders all Candidate,
  Review, Decision, path, and JSON values only as text nodes.
- The Preview page contains exactly one future publish button and no modal.
  It is enabled only for `publishable`, disabled for `stale` and
  `registry_unavailable`, and has no handler or publication mutation in Task 5.

## Files

Backend and composition:

- `src/zdecision/central/web/previews.py`
- `src/zdecision/central/web/store.py`
- `src/zdecision/central/web/application.py`
- `src/zdecision/central/web/api.py`
- `src/zdecision/central/api.py`
- `src/zdecision/central/cli.py`

Frontend:

- `web/src/pages/publication-preview/PublicationPreviewPage.tsx`
- `web/src/pages/candidate-review/CandidateReviewPage.tsx`
- `web/src/api/types.ts`
- `web/src/app/router.tsx`
- `web/src/styles/app.css`

Tests:

- `tests/test_central_web_preview.py`
- `tests/test_central_web_api.py`
- `web/src/pages/publication-preview/PublicationPreviewPage.test.tsx`
- `web/src/pages/candidate-review/CandidateReviewPage.test.tsx`

## RED evidence

Backend domain command before implementation:

```text
.venv/bin/python -m unittest tests.test_central_web_preview -v
```

Failed with the expected
`ModuleNotFoundError: No module named 'zdecision.central.web.previews'`.

Preview transport commands before application/route composition:

```text
.venv/bin/python -m unittest \
  tests.test_central_web_api.CentralWebApiTest.test_preview_routes_return_exact_safe_artifact_and_replay \
  tests.test_central_web_api.CentralWebApiTest.test_preview_routes_are_strict_and_do_not_create_publications -v
```

Failed because `CentralWebApplication` did not accept Preview Registry
dependencies and exposed no Preview operation.

Independent page command before implementation:

```text
cd web && npm test -- PublicationPreviewPage.test.tsx
```

Failed 4/4 because the stable route still rendered the deferred-slice page and
had no exact files, inert Decision fields, status, or confirmation control.

Candidate-to-Preview command before navigation composition:

```text
cd web && npm test -- CandidateReviewPage.test.tsx \
  -t "submits an ordered partial accept"
```

Failed because the successful Review never sent the explicit Preview action.

Atomic-base race regression before the second proof:

```text
.venv/bin/python -m unittest \
  tests.test_central_web_preview.CentralPreviewServiceTest.test_base_change_before_atomic_freeze_writes_no_preview_or_action -v
```

Failed because a simulated base change after render did not abort the freeze.

## GREEN and compatibility evidence

Final evidence is recorded from fresh commands immediately before commit in
the completion section below.

## Compatibility

- Existing Packet 1 dashboard, Candidate Inbox, draft, Review submission, and
  repository deep-link APIs remain unchanged. `CentralWebApplication` keeps
  Preview dependencies optional for existing test/embedding callers, while
  the production central CLI supplies the real Catalog and Git adapter.
- Reused the current immutable SQLite schema; no migration or mutable Preview
  replacement was added.
- Existing V1 Decision, Registry, publication-record, ID, path, and canonical
  JSON contracts are unchanged.
- Focused compatibility includes central Web Store, Review, query, Registry,
  and Git adapter suites plus Candidate Review frontend tests. Complete suites
  remain reserved for Task 8 by the brief.

## Self-review

- Confirmed accepted content only; rejected/skipped text and Review notes do
  not occur in display documents, changed files, Decision panels, or API JSON.
- Confirmed all authority and formal identities are server derived, and read
  ownership requires both organization and the Review batch actor.
- Confirmed commit-bound archives ignore replacement refs and worktree/index
  tricks; the actual Registry tree is unchanged before/after Preview creation.
- Confirmed the base proof, latest/unpublished proof, immutable Preview insert,
  and action replay insert share the final immediate transaction. A late base
  change rolls back both rows.
- Confirmed exact action replay returns the frozen ID and changed request bytes
  conflict. A second action that deterministically names the same Preview also
  reuses the original frozen timestamp and record rather than conflicting;
  Registry failure writes neither Preview nor replay row.
- Confirmed freshness compares Review/Candidate identities, Decision IDs,
  base digests, changed paths, complete display bytes, and complete changed
  bytes without mutating the stored record.
- Confirmed the read distinction: known changed base is stale; unreachable
  Registry is unavailable; both preserve the same frozen record.
- Confirmed the page has one button, no dialog, no event handler that can
  publish, complete visible/expandable canonical text, and no HTML injection.
- Confirmed no Task 6 publication row, confirmation approval, receipt, Registry
  write, Git commit, push, resume, or recovery behavior was implemented.

## Concerns

- Focused FastAPI tests emit the existing Starlette/httpx deprecation warning;
  it does not affect results and dependency migration is outside Task 5.
- As in Task 4, the packaged SPA asset rebuild and complete backend/frontend
  suites remain later plan work; this task verifies source, types, and focused
  browser behavior only.

## Completion evidence

Fresh backend Task 5 plus directly affected compatibility command:

```text
.venv/bin/python -m unittest \
  tests.test_central_web_preview tests.test_central_web_api tests.test_registry \
  tests.test_central_web_store tests.test_central_web_review \
  tests.test_central_web_queries tests.test_git_registry -v
```

Exit 0, 77/77 passed. This includes accepted-only exact bytes, no Registry
write, replay/conflict, late-base rollback, Review/Candidate/Registry
staleness, Registry-unavailable reads, strict API output, immutable store
compatibility, Review ordering, commit-bound query regressions, and existing
Git safety behavior.

Fresh frontend command:

```text
cd web && npm run typecheck && \
  npm test -- PublicationPreviewPage.test.tsx CandidateReviewPage.test.tsx
```

Exit 0. TypeScript passed and 17/17 tests passed across the exact Preview page
and directly affected Candidate Review flow.

Additional fresh verification:

- `.venv/bin/python -m compileall -q src tests`: exit 0.
- `git diff --check`: exit 0.

## Independent code-review disposition

The required independent review found three actionable issues. All were
accepted, fixed, and covered before the final verification above:

- Multiple action IDs that derive the same deterministic Preview ID now reuse
  the original frozen `created_at` and exact immutable record.
- Preview request failures now preserve the Review batch ID and exact Preview
  action ID durably and expose a retry that cannot duplicate Review submission.
- The complete Decision panel now renders the Review approval actor explicitly.

The reviewer reported no further architectural, security, or Task 5 scope
concerns beyond those resolved findings.
