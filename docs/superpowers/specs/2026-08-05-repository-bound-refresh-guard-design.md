# Repository-Bound Candidate Refresh Guard Design

**Status:** Approved for implementation

## Problem

ZDecision already prevents a missing control binding from creating a Capture
Request, but its presentation boundary is too permissive:

- the Plugin Skill treats any exact `更新候选决策` input as authority to render;
- a delegated message can therefore redirect an unrelated running task before
  repository eligibility is checked; and
- when the `PreToolUse` Hook cannot bind an enabled repository, it currently
  allows the render call with empty input, producing a disabled card.

The observed incident interrupted a non-repository task and its child agents.
No Capture Request or Candidate mutation occurred, but that is insufficient:
ZDecision must not disturb an ineligible source task at all.

## Decisions

### 1. Native same-task authority only

The inline control has exactly two presentation boundaries: an exact, native
user message in the current Codex task, or the existing automatic presentation
after that same task reaches a completed and verified code-development
boundary. Both require the current task to be bound to an enabled repository.
The following never authorize rendering or Capture:

- a `<codex_delegation>` message or other cross-task envelope;
- `send_message_to_thread`, `turn/steer`, or equivalent task coordination;
- quoted text, retained summaries, tool output, Candidate text, or copied
  prompts; and
- a model deciding on its own to update an unrelated task.

If a delegated refresh phrase reaches a task, the Skill must not call any
ZDecision tool and must not replace the task's existing goal with the refresh
request.

### 2. Eligibility is checked before presentation

For an explicit native refresh phrase, the Skill first calls
`zdecision_status`. It may call `show_zdecision_update` only when all three
authoritative values are true:

```text
repository_registered
repository_enabled
active_session_bound
```

Otherwise it returns a bounded unavailable result without rendering the card,
creating a control binding, or starting Capture. It does not expose a Session
ID, filesystem path, repository identity, or detailed failure reason.

This status check is only an early rejection filter. It cannot grant a control;
the host-identity Hook check below remains authoritative.

The existing automatic presentation after a completed and verified code
boundary remains restricted to an enabled repository and an active local
Session binding.

### 3. The Hook is the deterministic backstop

The `PreToolUse` Hook continues to validate host-owned `session_id`, `turn_id`,
and `cwd`, resolve the Git repository, and verify the local mapping. It also
requires an already observed, non-ended local Session for that exact
`session_id` and `cwd`, with the current `turn_id` recorded by the lifecycle
Hook. A different Session sharing the same working directory is not a match.
When any check fails, it returns a blocking `permissionDecision: "deny"`; it
does not allow the MCP render tool to run with empty input.

Only a successful validation may return `permissionDecision: "allow"` with a
new private `control_id`. The MCP action tools continue to validate that
binding independently before creating a central request.

### 4. Cross-Session Capture remains read-only

`所有有效 Session` means changed, eligible interactive Sessions already bound
to the same registered and enabled repository as the control. Selection and
source reading occur through the local Agent and typed app-server gateway.

ZDecision must never send a prompt, delegation, follow-up, or steer operation
to a source Session. Extraction may run only in the existing isolated Capture
fork/turn path; source tasks remain untouched and may continue normally.

## Resulting flow

```text
native user Turn in current task
  -> zdecision_status
  -> registered + enabled + active binding?
       no  -> bounded unavailable response; no card
       yes -> show_zdecision_update
              -> PreToolUse revalidates host task + repository
              -> private control binding
              -> inline card
              -> explicit user scope click
              -> read-only same-repository source selection
              -> isolated Capture processing
```

A delegated or cross-task refresh phrase exits before `zdecision_status` and
does not alter the receiving task's goal.

## Changes

- Tighten `plugins/zdecision/skills/zdecision/SKILL.md` with the native-turn,
  eligibility, and no-cross-task rules.
- Change the invalid `PreToolUse` branch in
  `src/zdecision/agent/hooks.py` from allow-with-empty-input to deny.
- Add one narrow Agent-database query used by the Hook to prove the exact
  current Session/Turn/CWD observation instead of accepting another Session in
  the same directory.
- Keep `LocalMcpTools.show_zdecision_update` fail-closed as defense in depth;
  its no-binding result remains non-authoritative and cannot start Capture.
- Align the active architecture and inline-card specification with this
  amendment.
- Update the installed local Plugin only after repository tests pass; Codex
  restart remains required before the live smoke test.

## Verification

Automated tests must prove:

1. the Skill rejects delegated/cross-task refresh triggers and requires the
   three status gates before rendering;
2. unresolved, unregistered, disabled, mismatched, ended, unobserved,
   wrong-Turn, and subagent Hook inputs are denied and create no control
   binding;
3. a native enabled-repository task still receives one usable control;
4. a missing or forged control cannot create a Capture Request; and
5. `all_valid_sessions` selects only eligible Sessions from the bound
   repository and never sends to source tasks.

The bounded live smoke test is:

- in a no-repository task, a native `更新候选决策` request produces no card and
  no Capture Request;
- in an enabled-repository task, the same native request produces the card;
- clicking `所有有效 Session` creates exactly one repository-owned request;
  and
- an unrelated running task receives no message and continues uninterrupted.

## Non-goals

- Changing Codex App's generic cross-task messaging API.
- Adding a task scheduler or coordinator.
- Expanding repository discovery beyond the existing trusted binding rules.
- Changing Candidate extraction, Review, publication, or Decision schemas.
