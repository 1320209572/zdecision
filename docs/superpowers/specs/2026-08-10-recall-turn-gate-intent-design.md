# Recall Turn-Gate Intent Contract Fix

**Status:** Approved for implementation on 2026-08-10

## Problem

The active Recall `PreToolUse` Hook replaces the complete MCP arguments with
only a trusted `turn_gate_id`. That correctly removes model-authored host
coordinates, but it also deletes the required semantic `intent`. The MCP tool
then fails schema validation before the Recall gate can run.

The model-visible MCP schema also exposes `intent` as an unstructured unknown
value. Codex therefore cannot reliably construct the existing strict
`RecallIntent` contract.

## Decision

The Turn-gate call has two different classes of input:

- `turn_gate_id` is a host coordinate. The Hook always discards any
  model-authored value and injects the trusted bound value.
- `intent` is bounded semantic input authored from the current native task
  context. The Hook preserves exactly this field and no other model-authored
  field. The MCP boundary validates it against the existing seven-field
  `RecallIntent` contract before the provider runs.

The model-visible schema must name all seven fields:

- `target_decision_space_ids`
- `explicit_multi_space`
- `feature_goal`
- `domain_objects`
- `repository_relative_paths`
- `constraints`
- `exclusions`

Unknown fields remain forbidden. Prompt, PRD, transcript, source, diff, tool
output, Session ID, Turn ID, CWD, repository ID, and other host coordinates are
not copied into the Hook output or persisted as intent evidence.

## Alternatives rejected

1. Derive intent inside the Hook from the raw Prompt. Rejected because Hooks
   must remain deterministic, model-free, and outside Prompt persistence.
2. Freeze the initial intent when the confirmation card is shown. Rejected
   because confirmation and later development intent are separate concerns,
   and later native Turns may legitimately change intent.
3. Keep `intent: unknown` and rely on Skill prose. Rejected because the real
   Desktop trace proved the tool contract itself must be discoverable and
   machine-checkable.

## Failure behavior

- Missing or malformed `intent` denies the Gate call without committing it.
- The active Turn remains blocked until a valid Gate call commits.
- Replays continue to use the existing intent digest and binding rules.
- This fix does not add formal Decision retrieval. Gate 1 continues to use its
  readiness/acceptance provider boundary.

## Acceptance

1. A real-shaped PreToolUse call containing valid semantic intent and fake host
   coordinates returns replacement arguments containing only the same intent
   plus the trusted `turn_gate_id`.
2. Missing, malformed, or extra intent fields fail closed.
3. The model-visible MCP schema exposes the complete strict intent object and
   forbids unknown fields.
4. The rewritten arguments execute through the registered MCP tool without a
   missing-field error.
5. Existing activation, replay, privacy, and active-Turn backstop tests remain
   green.
