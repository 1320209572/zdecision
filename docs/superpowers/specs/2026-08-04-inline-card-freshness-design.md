# ZDecision Inline Card Freshness Amendment

Status: Approved for implementation planning

This amendment refines only the presentation and resource identity of the
approved Codex inline Candidate-refresh card. It does not change Capture,
Review, publication, repository eligibility, or Control Binding authorization.

The later `2026-08-05-repository-bound-refresh-guard-design.md` is
authoritative for whether a new card may be rendered. This document still
governs already-rendered valid and historical cards.

## 1. Observed problem

Real Codex Desktop acceptance on 2026-08-03 showed that returning to a task can
remount several historical inline cards at once. Host logs confirmed that all
three observed status calls completed successfully. The newest Control Binding
still returned `ready`, while older bindings could legitimately be expired.

The current widget maps both an expired historical control and a current
temporary failure to `暂时无法更新`. Because every card is visually identical,
the user cannot tell an expired historical card from the current usable card.
The widget resource has also changed repeatedly while retaining the same
`ui://zdecision/update-candidates-v1.html` identity, leaving room for a host to
reuse an older resource snapshot.

## 2. Product contract

The card distinguishes freshness without exposing repository-disable reasons
or private identifiers:

- a valid unselected Control Binding is labeled **当前卡片** and keeps the two
  approved scope actions;
- a status result with `safe_state: unavailable` and no persisted binding
  fields is an invalid or expired historical control, is labeled **历史卡片**,
  displays **此更新卡已失效**, and keeps both actions disabled;
- a valid current binding whose central request is temporarily unavailable
  retains the existing generic **暂时无法更新** presentation; and
- an unregistered, disabled, unresolved, or unobserved task does not render a
  new card under the repository-bound presentation guard.

Historical cards do not retry, create a replacement binding, or redirect an
action to the newest card. The user creates a new card through the existing
eligible native same-task **更新候选决策** flow.

## 3. Resource identity

The MCP Apps resource URI changes to
`ui://zdecision/update-candidates-v2.html`. The local source filename may remain
unchanged; the URI is the host-visible immutable resource identity. Tool
metadata and the compatibility output-template field must reference the same
new URI.

This is a cache-boundary change only. It does not introduce a second widget or
a protocol migration layer.

## 4. Verification

Focused automated tests must prove:

1. a valid `ready` result renders **当前卡片** and enables both actions;
2. an unbound or expired status result renders **历史卡片** with
   **此更新卡已失效** and never enables an action;
3. a current bound temporary failure still renders **暂时无法更新**;
4. the registered resource, render metadata, and compatibility metadata all
   use the v2 URI; and
5. the existing local-page link probe and all inline-card tests remain green.

Real acceptance uses one newly rendered card after Codex reload. It must be
visually identifiable as **当前卡片** before the local-link experiment
continues. No further broad card or host redesign belongs to this correction.
