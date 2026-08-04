# Task 7 report: formal Decision catalog and completed read models

## Status

Implemented the Task 7 read-only vertical slice on `main` from base `8ac0977`.
The slice adds commit-bound active V1 Decision catalog/detail queries, trusted
HTTP routes, read-only React pages, completed-publication metadata joins, and
company/product navigation links. No Decision mutation, lifecycle update,
comment, administration, analytics, recall, or Task 8 work was added.

## RED evidence

- Backend query tests failed with `AttributeError` for missing
  `RegistrySnapshot.active_decisions`, `CentralWebQueries.list_decisions`, and
  `CentralWebQueries.get_decision`.
- HTTP tests failed with 404 route misses for the catalog and product-owned
  detail endpoints.
- Frontend tests failed on the deferred Decision routes, missing inert Decision
  content, missing Registry-unavailable state, and missing product/publication
  cross-links.
- A self-review regression changed receipt `recorded_at` to its real earlier
  confirmation timestamp; the focused query test then reproduced
  `WebRecordCorrupt`. The root cause was an invalid equality assumption between
  immutable receipt time and the later completed-publication `updated_at`.

## GREEN evidence

- `.venv/bin/python -m unittest tests.test_central_web_queries tests.test_central_web_api tests.test_registry -v`
  passed 37 tests.
- `cd web && npm test -- DecisionCatalogPage.test.tsx DecisionDetailPage.test.tsx CompanyOverviewPage.test.tsx PublicationHistoryPage.test.tsx`
  passed 6 tests across 4 files.
- `cd web && npm run typecheck` completed with exit code 0.
- `git diff --check` completed with exit code 0.

## Files

- Registry/read model: `src/zdecision/registry/query.py`,
  `src/zdecision/central/web/queries.py`.
- Application/HTTP: `src/zdecision/central/web/application.py`,
  `src/zdecision/central/web/api.py`, `src/zdecision/central/api.py`.
- Frontend: `web/src/api/types.ts`, `web/src/app/router.tsx`,
  `web/src/pages/decision-catalog/`,
  `web/src/pages/company-overview/CompanyOverviewPage.tsx`,
  `web/src/pages/publication-history/PublicationDetailPage.tsx`, and
  `web/src/styles/app.css`.
- Tests: focused Central Web query/API tests and Decision catalog/detail,
  company overview, and publication-detail frontend tests.

## Compatibility and boundaries

- Each dashboard, catalog, or detail call obtains one commit-bound Registry
  snapshot and never substitutes worktree bytes.
- Catalog results include only active V1 revisions owned by enabled server-side
  product mappings. Product/Decision mismatch returns 404.
- Search and repository filters are capped at 200 UTF-8 bytes; pagination is
  limited to 1–100 with non-negative offsets; publication time accepts only
  timezone-qualified RFC 3339.
- Completed publication metadata is joined through organization/product,
  preview, receipt Decision identity, and exact commit SHA. The displayed
  publication time comes from the completed publication row.
- Registry unavailability remains explicit: query views carry unavailable with
  `items=None`, while HTTP returns 503 `registry_unavailable`; dashboard
  Candidate/product counts remain available and active counts become null.
- Detail responses expose the complete canonical formal document and safe
  opaque provenance as text. The UI has no mutation controls and renders formal
  content inertly.
- Existing Candidate Review, Preview, publication/recovery, history, dashboard,
  SPA fallback, Packet 1, and Tasks 2–6 focused compatibility tests remain
  passing.

## Self-review

- Confirmed organization and product ownership is preserved in every database
  join and Registry lookup.
- Confirmed only completed publication rows can annotate formal Decisions.
- Confirmed unavailable and empty UI states are distinct and no hard-coded
  ZStack product catalog was introduced.
- Confirmed product overview links cover Candidate, Decision, and publication
  routes; recent publication and publication-detail Decision links use stable
  server identities.
- Corrected the receipt/completion timestamp mismatch discovered during review;
  receipt identity checks remain strict without conflating two lifecycle times.

## Concerns / deferred verification

- FastAPI focused tests emit the existing Starlette `httpx` deprecation warning;
  it does not fail the suite and was not changed in this slice.
- Per the Task 7 brief, full repository suites, packaged frontend rebuild, and
  real end-to-end acceptance are intentionally deferred to Task 8.

## Fix round 1

Two Important query-review findings were corrected with focused RED→GREEN
regressions:

- Publication metadata now carries its trusted `preview_id` projection and is
  attached to a formal Decision only when organization, product, Decision,
  revision `publication_preview_id`, completed publication, receipt, and exact
  commit all agree. A mismatched preview contributes no metadata and cannot
  satisfy `published_after`.
- Dashboard product and global active counts now require the Registry revision
  product name to equal the enabled server mapping name, matching catalog and
  detail ownership behavior. A same-ID/name-mismatched revision is not counted.

RED evidence: the preview-mismatch regression incorrectly received a
publication ID, while the dashboard ownership regression counted one active
Decision. GREEN evidence: both regressions pass, and
`.venv/bin/python -m unittest tests.test_central_web_queries tests.test_central_web_api -v`
passes 29 tests. The HTTP/dashboard response shape did not change, so no
frontend overview implementation or test change was required.
