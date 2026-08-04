# Task 6 report — explicit publication and exact recovery

## Status

Implemented Task 6 only on the existing `main`, based on `4e2b553`. Publication
starts only from the preview-page confirmation action. Frozen Preview records and
their exact bytes are read but never rewritten.

## RED → GREEN evidence

- RED backend: `.venv/bin/python -m unittest tests.test_central_web_publication -v`
  failed with `ModuleNotFoundError: zdecision.central.web.publications` before the
  publication service existed.
- GREEN backend: the focused Task 6 command passed 48/48 tests:
  `.venv/bin/python -m unittest tests.test_central_web_publication tests.test_central_web_api tests.test_git_registry tests.test_registry -v`.
- RED frontend: `npm test -- PublicationPreviewPage.test.tsx PublicationHistoryPage.test.tsx`
  failed on the missing publish action and deferred history/detail routes.
- GREEN frontend: the same command passed 7/7 tests. `npm run typecheck` also
  completed with exit code 0.
- Direct compatibility: `.venv/bin/python -m unittest tests.test_central_web_store tests.test_central_web_queries tests.test_central_web_preview -v`
  passed 28/28 tests.

## Crash and recovery evidence

- Crash after confirmation leaves `state=confirmed`, no SHA, and no Git commit.
- Crash after exact commit but before state persistence adopts the same child
  commit on resume without creating another commit.
- Crash after frozen bytes are written but before commit safely reuses only those
  exact bytes and creates one exact commit.
- Unknown push verification returns `committed_pending_push`; resume proves the
  same SHA on `origin/main` and completes without recommitting.
- Crash after the remote push but before `completed` is recovered through
  `publication_remote_state == contains`.
- Unrelated remote state latches `recovery_code=ambiguous`, preserves the durable
  monotonic state, and rejects every later automatic resume.
- Git proof ignores replace refs and inherited alternate index/worktree/object
  overrides. Existing unrelated staged files remain outside the exact commit.

## Files and behavior

- Added `src/zdecision/central/web/publications.py` for confirmation, replay,
  resume, exact commit adoption/push proof, history, and detail projections.
- Extended Web store CAS, family ownership, receipt reads, history pagination,
  and ambiguity latching; dashboard queries project ambiguity explicitly.
- Added strict publish/resume/history/detail Web APIs and stable 404/409 errors.
- Existing CLI composition already passed the same trusted `RegistryCatalog` and
  `GitRegistryAdapter` pair; no CLI behavior or command surface was added.
- Added publication history/detail pages and enabled the single Preview publish
  button. Pending push is never labeled completed; ambiguous state has no retry.
- History includes only IDs, counts, Demo actor, approval time, durable state,
  recovery code, and SHA—never rejected/skipped Candidate content or Review notes.

## Compatibility and self-review

- Packet 1 and Tasks 2–5 focused Store/Query/Preview behavior remain green.
- Local tests use disposable bare remotes only. This development repository was
  never pushed.
- Self-review checked organization/product visibility, browser-body strictness,
  immutable Preview binding, action replay digests, one receipt per family,
  monotonic SHA/state transitions, and exact `origin/main` proof.
- No Task 7+ work was started. Per the brief, full suites remain for Task 8.

## Concerns

- FastAPI emits the repository's existing `StarletteDeprecationWarning` for
  `TestClient`; it does not affect test results or Task 6 behavior.
