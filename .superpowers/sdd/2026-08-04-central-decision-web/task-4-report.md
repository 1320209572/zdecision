# Task 4 implementation report

## Status

Implemented all-or-nothing immutable product Review submission, strict Review
transport, current-head stale detection, replay/conflict handling, partial
Review state, and browser submit/reconciliation behavior. No publication
preview record, publication, Registry write, or Task 5+ operation was added.

## Implementation

- Added `CentralReviewService.submit` around one `BEGIN IMMEDIATE`
  transaction. It checks the server-derived browser actor, organization,
  product, enabled repository ownership, exact saved-draft CAS version and
  items, and every exact current Candidate head before the first immutable
  Review insert.
- Added strict current-head loading and sanitized `ReviewStale` family IDs.
  Any missing or changed head rolls the whole transaction back and leaves the
  durable draft intact.
- Froze current Candidate content for `accept`, complete validated submitted
  content for `edit_accept`, and no content for `reject` or `skip`. Batch,
  publication-Candidate, Review-item, and approval identities use the existing
  stable adapters and server-derived authority.
- Reused the existing immutable `CentralWebStore.put_review_batch`, draft
  clearing CAS, and action-result primitives inside the owning transaction.
  Identical action bytes replay the original batch; changed bytes conflict.
- Derived latest state so `accept`/`edit_accept` are preview-eligible,
  `reject` is processed, and `skip` remains pending. Review submission never
  writes `web_publication_previews`.
- Added strict `POST /api/v1/web/products/{product_id}/reviews` composition and
  409 mappings for stale Review and Web action conflict. The response exposes
  ordered safe identities/actions, eligibility, pending count, and incremented
  draft version; it omits notes and Candidate/Review content.
- Added the Candidate Review primary action with action-dependent wording,
  disabled empty state, automatic durable draft save when local edits differ,
  safe status transitions, stale-row marking, explicit latest-version loading,
  retained selections, and no automatic resubmission.

## Files

Backend:

- `src/zdecision/central/web/reviews.py`
- `src/zdecision/central/web/queries.py`
- `src/zdecision/central/web/application.py`
- `src/zdecision/central/web/api.py`
- `src/zdecision/central/api.py`

Frontend:

- `web/src/api/client.ts`
- `web/src/api/types.ts`
- `web/src/features/reviews/ReviewEditor.tsx`
- `web/src/pages/candidate-review/CandidateReviewPage.tsx`

Tests:

- `tests/test_central_web_review.py`
- `tests/test_central_web_api.py`
- `web/src/pages/candidate-review/CandidateReviewPage.test.tsx`

The existing `src/zdecision/central/web/store.py` Task 1 APIs already supplied
the required immediate-transaction reuse, immutable insert, exact draft clear,
and action replay primitives, so no new persistence API or schema change was
needed.

## RED evidence

Backend service command:

```text
.venv/bin/python -m unittest tests.test_central_web_review.CentralWebReviewTest.test_partial_review_records_only_classified_items -v
```

Failed before production implementation with the expected
`AttributeError: 'CentralReviewService' object has no attribute 'submit'`.

Transport command:

```text
.venv/bin/python -m unittest tests.test_central_web_api.CentralWebApiTest.test_review_route_returns_only_safe_submission_results -v
```

Failed before the route was added with expected HTTP 405 instead of 200.

Frontend command:

```text
cd web && npm test -- CandidateReviewPage.test.tsx
```

The three new submission tests failed before UI implementation because the
required `生成发布预览` / `提交审核结果` controls did not exist.

## GREEN and compatibility evidence

Fresh backend focused plus directly affected compatibility command:

```text
.venv/bin/python -m unittest tests.test_central_web_review tests.test_central_web_api tests.test_central_web_queries tests.test_central_web_store tests.test_central_api -v
```

Exit 0, 52/52 passed. This covers Review atomicity/replay/state, strict Web
transport, current-head/dashboard query compatibility, Task 1 immutable store
contracts, and Packet 1 central API compatibility.

Fresh frontend command:

```text
cd web && npm run typecheck && npm test -- CandidateReviewPage.test.tsx
```

Exit 0. TypeScript passed and Candidate Review passed 11/11, including partial
accept/reject, reject-only, stale reconciliation without resubmission, and
hostile markup rendered only as text.

Additional verification:

- `.venv/bin/python -m compileall -q src tests`: exit 0.
- `git diff --check`: exit 0.
- Full backend/frontend suites were intentionally not run; the brief reserves
  them for Task 8.

## Self-review

- Confirmed all mutable checks complete before `put_review_batch`; nested store
  calls reuse the owning immediate transaction, so draft/action failures roll
  back the immutable batch and items.
- Confirmed exact current heads are joined by organization, repository, family,
  revision number, and revision ID, then canonical record/storage identity and
  request digest are revalidated.
- Confirmed request actor/organization never come from the body and route
  product/repository ownership is proved from enabled server mappings.
- Confirmed accepted content is frozen exactly once, reject is terminal, skip
  stays pending, partial unclassified families remain pending, and no preview
  table write or Registry operation exists in the submission path.
- Confirmed action digest binds product, draft version, ordered items, notes,
  and complete edit content; changed bytes cannot replay.
- Confirmed stale responses contain only validated family IDs and the UI keeps
  local/durable choices, loads latest identities explicitly, and never retries
  Review approval automatically.
- Confirmed API item results omit internal note and untrusted Candidate/Review
  text; React continues to render Candidate values as text nodes only.

## Concerns

- Focused FastAPI tests emit the existing Starlette `httpx` deprecation warning;
  it does not affect results and dependency migration is outside Task 4.
- No other known concern. Packaged SPA rebuild and complete suites remain later
  plan work; Task 5+ was not started.
