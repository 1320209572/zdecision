# Task 6 Report: Compact Leaf Candidate List and Batch Review

## Summary

- Replaced the card/select `ReviewEditor` with compact, accessible candidate
  rows. Every row has an independent Checkbox, unambiguous direct actions,
  inline edit-only expansion, and native evidence disclosure that starts
  collapsed.
- Added transient select-current-page state, exact accept/reject batch actions,
  and one-level undo that restores each touched family to its exact prior
  value, including unclassified and edited-accept states.
- Enforced the 20-classification boundary before both batch and row state
  changes. A 21-row selection remains intact and visible; the UI disables the
  mutation and explains the limit instead of truncating it.
- Review submission now includes only explicitly classified, current,
  non-stale revisions in Inbox order. Checkbox selection never enters the
  payload. Review submission and Preview generation are separate explicit
  actions while preserving the durable Preview retry identity.
- Material filter and leaf route changes clear only transient selection. Local
  draft actions remain present for the same leaf, and a different leaf adopts
  only that leaf's server draft.
- Added sticky accepted/rejected/unprocessed/stale counts and an industrial,
  editorial batch console consistent with the existing ZStack central board.
  Mobile layouts collapse the row and evidence ledger without adding new
  dependencies or animation systems.

## RED evidence

The first exact focused run was made before production implementation:

```text
npm test -- src/pages/candidate-review/CandidateReviewPage.test.tsx src/features/reviews/CandidateReviewRow.test.tsx
Test Files  2 failed (2)
Tests       5 failed | 13 passed (18)
```

The page failures named the missing row Checkbox, direct actions, batch
toolbar, exact undo, 21-item guard, and selection-preserving filter behavior.
The row suite failed import resolution because `CandidateReviewRow` did not
yet exist. The old UI still exposed three `审核动作` selects, including the
primary Skip option.

An intermediate GREEN attempt proved the new row in isolation while retaining
expected integration gaps:

```text
CandidateReviewRow.test.tsx  3 passed
CandidateReviewPage.test.tsx 7 passed | 11 failed
```

Those remaining failures were legacy assertions for the removed select,
always-visible provenance, and the former combined Review/Preview button, plus
two ambiguous text queries introduced by the persistent summary. The
assertions were migrated to the approved Task 6 interaction rather than
restoring legacy behavior.

## GREEN evidence

Exact Task 6 focused command:

```text
npm test -- src/pages/candidate-review/CandidateReviewPage.test.tsx src/features/reviews/CandidateReviewRow.test.tsx
Test Files  2 passed (2)
Tests       22 passed (22)
```

Full affected Web regression suite:

```text
npm test
Test Files  9 passed (9)
Tests       40 passed (40)
```

Type and production bundle checks:

```text
npm run typecheck
exit 0

npm run build
43 modules transformed
src/zdecision/central/static/assets/index-BjnIEpFz.css
src/zdecision/central/static/assets/index-On9XTpWE.js
exit 0
```

Final recovery checks after a network interruption:

```text
git diff --check
exit 0

production source/static scan for ReviewEditor, 审核动作, 跳过,
and authored dangerouslySetInnerHTML usage
no matches
```

The network interruption occurred after the focused, full Web, typecheck, and
build commands had completed. Recovery re-read the shared worktree, confirmed
both new row files, the deleted editor, and both new static assets before
continuing. No changes were rewritten or discarded, and the already-complete
full/build commands were not repeated.

## Files changed

- Added `web/src/features/reviews/CandidateReviewRow.tsx` and its focused tests.
- Reworked `web/src/pages/candidate-review/CandidateReviewPage.tsx` and its
  interaction/API tests.
- Added the explicit classified-action type in `web/src/api/types.ts`.
- Replaced card/editor styles with compact row, sticky summary, batch toolbar,
  evidence, edit, and responsive styles in `web/src/styles/app.css`.
- Deleted `web/src/features/reviews/ReviewEditor.tsx`.
- Rebuilt `src/zdecision/central/static/`, replacing the old hashed CSS/JS
  assets and updating `index.html`.

## Self-review

- Confirmed every Checkbox changes only `selectedFamilyIds`; classification is
  created only by a direct or batch action.
- Confirmed a batch snapshots each selected family's exact `ReviewDraftItem |
  undefined`, a later batch replaces that one snapshot, and undo does not
  recompute prior values.
- Confirmed the 21-row tests cover both select-current-page batch blocking and
  disabling the twenty-first row's accept/reject/edit controls before state
  changes.
- Confirmed submit eligibility checks action, repository, revision ID,
  revision number, digest, and stale state against the currently displayed
  leaf candidates. Skip and selection-only rows are excluded.
- Confirmed Review POST targets only
  `/api/v1/web/spaces/{decision_space_id}/reviews`; Preview POST occurs only
  after a separate user action and reuses its durable action identity on retry.
- Confirmed evidence uses native `<details>`, Candidate text is rendered as
  React children, the edit panel keeps Decision-space and repository fields
  read-only, and row/action accessible names include the claim.
- Confirmed production sources and built assets contain no old Review editor,
  Review-action select, or Skip command. No backend contract or Task 7 file was
  changed.

## Risks

- The private draft API intentionally permits up to 100 saved choices while a
  Review batch permits 20. The UI therefore preserves an already-large draft
  but disables any mutation or submission that would violate the 20-item
  Review boundary; it never silently drops draft entries.
- Unsaved same-leaf choices intentionally win when a material filter refreshes
  the Inbox. The existing draft compare-and-swap still detects a concurrent
  remote update at save or submit time.

## Fix Round 1: Single-row transient editing

### Summary

- Separated editor visibility and field values from durable review actions.
  The page now owns one `CandidateEdit` buffer, and each row receives explicit
  controlled `editing` and `editContent` props.
- Opening editor A and then editor B discards A's unsaved buffer and leaves
  only B open. Opening, switching, canceling, filter/leaf changes, candidate
  refresh, direct actions, and batch actions do not turn a candidate into
  `edit_accept`.
- Added explicit accessible `保存并接受` and `取消` actions. Only the save
  boundary writes the buffered content as `edit_accept`; cancel leaves both
  the local draft and the next draft PUT unchanged.
- Preserved the 20-item boundary: an unclassified twenty-first row cannot open
  or save an editor, while any of the existing 20 classified rows remains
  editable without increasing the classified count. Decision-space and
  repository inputs remain read-only.

### RED evidence

The new page tests were run before changing production code:

```text
npm test -- --run src/pages/candidate-review/CandidateReviewPage.test.tsx
Test Files  1 failed (1)
Tests       3 failed | 17 passed (20)
```

The failures showed the previous action-derived panel, immediate
`edit_accept` mutation, missing explicit save/cancel controls, and editor state
remaining attached to a material filter refresh.

### GREEN evidence

```text
npm test -- --run src/pages/candidate-review/CandidateReviewPage.test.tsx src/features/reviews/CandidateReviewRow.test.tsx
Test Files  2 passed (2)
Tests       24 passed (24)

npm test
Test Files  9 passed (9)
Tests       42 passed (42)

npm run typecheck
exit 0

npm run build
43 modules transformed
src/zdecision/central/static/assets/index-DoM6JbCG.css
src/zdecision/central/static/assets/index-CrMsAOpJ.js
exit 0
```

### Self-review

- Confirmed no panel visibility is derived from `draft.action`; restored
  `edit_accept` actions display their classification without auto-opening.
- Confirmed transient edits never enter `draftByFamily` until explicit save,
  and the page test inspects the actual draft PUT after cancel and after save.
- Confirmed row switching replaces the single buffer, and material route,
  leaf, reload, latest-version, direct, and batch boundaries clear it.
- Confirmed this round changes only Task 6 Web source, tests, styles, report,
  and rebuilt static assets. Backend and Task 7 files remain untouched.
