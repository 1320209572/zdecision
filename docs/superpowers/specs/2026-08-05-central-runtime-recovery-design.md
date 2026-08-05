# Central Runtime Recovery Design

**Status:** Approved for implementation planning.

**Scope:** Repair the proven Demo outage in which the inline Candidate-refresh
card freezes a valid action locally but the loopback central Web service is not
running, and bound the card's same-mount recovery traffic.

**Amends:** `2026-07-31-codex-inline-candidate-refresh-design.md`. That design
remains authoritative for trusted Control Bindings, one-scope authorization,
central request idempotency, and Candidate progress. This amendment changes
only Demo startup documentation and the retry schedule for a durable pending
submission.

## 1. Observed failure

The accepted inline action reached the local MCP server and atomically stored:

```text
chosen_scope = current_session
client_action_id = codex_action_...
submission_state = pending
central_request_id = null
```

The action was not cancelled and the MCP process did not restart. The installed
Demo LaunchAgent repeatedly invoked `zdecision-central run` without the now
required `--registry-repository-root` argument. It exited with status 2 every
ten seconds, leaving no listener on `127.0.0.1:8765`.

The card then replayed the durable pending action every 1.5 seconds without an
attempt limit. Several mounted cards amplified this into hundreds of local MCP
calls while the service remained unavailable.

## 2. Selected repair

### 2.1 Keep the current pending identity

Do not clear the chosen scope, generate another action ID, or create a second
Control Binding. After the central service is healthy, the existing
`client_action_id` is replayed through the current central idempotency contract.
The central request is then attached to the original binding.

### 2.2 Repair the Demo startup contract without adding a product service manager

The current local LaunchAgent is an acceptance-environment convenience, not a
Plugin product surface. This slice therefore does not add a general
`zdecision-central service install` command or a second launchd abstraction.

The repair has two durable parts:

1. the README's executable `zdecision-central run` example must include the
   absolute `--registry-repository-root` argument already required by the CLI;
2. a focused contract test must fail if that documented startup boundary loses
   the argument again.

For the live acceptance environment, replace only the owned
`com.zdecision.central.demo` LaunchAgent arguments with the same absolute
Registry checkout, reload it, and require a successful loopback health check.
No secret is added to the plist.

### 2.3 Bound same-mount pending retries

One mounted card may automatically replay a durable pending action at these
delays after the initial submission:

```text
1.5s, 3s, 6s, 12s, 24s, 48s
```

There is at most one pending retry timer per mount. A successful attachment or
terminal result cancels the remaining schedule. After the sixth retry remains
pending, the card stops issuing tool calls and shows the existing generic
temporary-unavailable state.

Remounting the same card starts one new bounded recovery window and reuses the
same persisted scope and action ID. It never resets central identity. This
preserves recovery after a later service restart without allowing an
unbounded background request loop.

Attached request progress keeps its existing polling contract; this change is
limited to pre-attachment `pending` recovery.

## 3. Failure handling

| Failure | Required result |
|---|---|
| Central listener absent | Preserve `pending`; run only the bounded retry schedule |
| Central returns the existing idempotent request | Attach it to the original binding |
| Central creates the request but the response is lost | Replay the same action ID; never create a second logical request |
| Local attach fails after central success | Preserve `pending`; replay the same action ID in the bounded window |
| Retry budget exhausted | Stop automatic calls and show the generic unavailable state |
| Card remounted later | Start a fresh bounded window against the same durable action |

## 4. Verification and stopping rule

The implementation is complete when all of the following hold:

1. a failing test proves the README startup example cannot omit
   `--registry-repository-root`;
2. widget tests prove the exact bounded delays, one timer at a time, exhaustion,
   remount recovery, and cancellation after attachment;
3. focused launch/documentation and inline-card tests pass;
4. the complete existing test suite passes once;
5. the live LaunchAgent remains loaded with exit status 0 and port 8765 is
   listening; and
6. the already-persisted action attaches to exactly one new central Capture
   Request without asking the user to click again.

Stop after this acceptance. Do not add a central service installer, redesign
the central deployment model, or start another broad review.
