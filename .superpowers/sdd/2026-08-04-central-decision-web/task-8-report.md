# Task 8 Report — Central Decision Web Acceptance and Packaging

## Status

Automated Gates 1–6 are green on the required base `b0eb23e`. Gate 7's
Codex-card/default-browser flow is an explicit manual handoff: it needs a live
enabled Codex task and publication from the user's Demo checkout, which this
automated run was not authorized to perform. Nothing was pushed.

## Disposable vertical evidence

Fresh final run: 1 test, 0 failures, 0 errors.

| Artifact | Disposable ID |
| --- | --- |
| Capture Request | `crq_0f859a46656191d66248e3cc84193bdf` |
| Review batch | `rvb_28d371803176766ecd4de77ffb4fcd3f` |
| Preview | `pub_fc9ede0b18d02e1cfceee9ec3a972345` |
| Publication | `plb_e81e6a5844c5283ccb2c7877b068a1a6` |
| Decision | `dec_4b829008aeb189ea27e3fc1754589fbb` |
| Temporary remote commit | `8d0616b67510949bd088fcc86e00a5d5f41cb489` |

The fixture used one temporary working repository, one temporary bare origin,
exact `main`, a persistent SQLite file, the fixed Demo identity, and the real
FastAPI transport. It created and completed Capture through the agent HTTP
routes, saved a partial accept/reject/skip draft, restarted and restored it,
submitted Review, froze Preview, restarted again, injected a crash after the
Git commit, restarted, and completed the same publication. The temporary bare
remote contained the exact commit, the product Decision file existed, the
Decision detail and publication history resolved over HTTP, one preview commit
existed, and one accepted-family receipt existed.

## Privacy and negative-boundary evidence

Every forbidden field (`organization_id`, `actor_id`, `product_name`,
`registry_path`, `commit_message`, `decision_bytes`, `session_id`, and
`prompt`) was sent to each relevant Capture, draft, Review, Preview, publish,
and resume mutation. Each returned the stable `422 {"error":"invalid_request"}`
without echoing its unique value.

After a WAL checkpoint, all unique raw-Prompt, source-code, diff, credential,
local-path, Decision-bytes, Session, and Prompt sentinels were absent from the
SQLite database bytes, captured HTTP response JSON, and all reachable Git
blobs. The rejected and skipped Candidate claim sentinels were absent from
every Git blob.

## Verification evidence

- Focused vertical: `python -m unittest tests.integration.test_central_web_vertical -v` — 1/1 passed.
- Browser/API compatibility: 15/15 passed across
  `tests.test_update_candidates_page` and `tests.test_central_web_api`.
- Complete backend discovery: 605 tests discovered and the fresh complete run
  exited 0; 2 existing live-environment tests skipped, 0 failures/errors.
- Frontend: TypeScript passed; Vitest 6/6 files and 27/27 tests passed.
- Unsafe-code scan for `dangerouslySetInnerHTML`, `innerHTML`, `eval(`, and
  `new Function` under `web/src` returned no matches.
- Production build: Vite transformed 40 modules and generated
  `index-CuOAgVv1.css` (32.83 kB, 7.40 kB gzip),
  `index-CUym1esE.js` (333.24 kB, 102.18 kB gzip), and `index.html`
  (0.45 kB, 0.29 kB gzip).
- `git diff --check` returned no output.

## Visual and route status

A disposable loopback app containing Candidate, Preview, Decision, and
Publication fixtures was inspected in the in-app browser at 1440×1000 and
390×844. Exactly these eight routes were visited at both widths: `/`,
`/reviews`, product Candidate, Preview, `/decisions`, Decision detail,
`/publications`, and Publication detail. Desktop layouts had no horizontal
overflow. The narrow pass found and bounded three concrete defects: decorative
horizontal overflow, missing owning-navigation selection on nested routes, and
one-character Preview metadata wrapping. The fixes clip only decorative
overflow, select the owning navigation section, collapse Preview metadata at
the existing mobile breakpoint, and correct the visible ZStack wordmark from
`ZETACK` to `ZSTACK`. Focus styling remains the global `:focus-visible`
contract. The temporary browser tab, viewport override, HTTP server, working
repository, bare origin, database, and published Registry were all cleaned up.

## Files and concrete Gate fixes

- Added the vertical fixture and exact operator runbook:
  `tests/integration/test_central_web_vertical.py`,
  `docs/demo-central-web.md`.
- Tightened route/security evidence:
  `tests/test_central_web_api.py` and Candidate/Preview/Decision frontend tests.
- Fixed observed transport contract failures: underscore-compatible documented
  web action IDs, and safe publish/resume HTTP views containing Decision IDs.
- Fixed observed visual failures in `AppShell.tsx`, `tokens.css`, `app.css`, and
  `zstack-logo.svg`.
- Regenerated `src/zdecision/central/static/index.html` and the two hashed SPA
  assets listed above; replaced the prior hashed bundle files.

## Manual Gate 7 handoff and remaining concerns

Use `docs/demo-central-web.md` to perform one live flow and record its Capture
Request, Review, Preview, Publication, Decision, and commit IDs. The flow must
verify the inline **更新候选决策** card, **所有有效 Session**, default-browser
opening without a Session ID, restart restoration, one confirmation click, and
the final Decision/history links. It must not be represented as complete until
those IDs are recorded.

The instructed local base `b0eb23e` is currently behind the checkout's
`origin/main`; therefore the runbook's exact synchronized-main prerequisite is
not currently met and must be resolved by the user/main coordinator before the
live Gate 7 flow. The full suite also emits an existing FastAPI TestClient
deprecation warning, and one run emitted an existing SQLite `ResourceWarning`;
neither caused a failure. SSO, Git-role authorization, Decision updates, and
automatic recall remain expressly outside this Demo.
