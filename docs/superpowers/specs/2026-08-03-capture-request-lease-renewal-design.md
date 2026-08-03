# Capture Request Lease Renewal Design

**Status:** Draft for written review. The independent renewal direction was
confirmed on 2026-08-03; implementation remains blocked until this written
contract is accepted.

**Scope:** One focused correction to Gate C of the approved on-demand Candidate
refresh design. This document does not change Candidate extraction, model
profiles, Review, publication, the inline controls, or the 30-second central
lease policy.

## 1. Confirmed defect

The central service grants a 30-second Capture Request lease at `claim` and
renews it only when the Agent calls `heartbeat`. The local Capture pipeline can
block for as long as 300 seconds inside one app-server structured Turn.

The current Agent sends heartbeats before and after those blocking calls. It
therefore implements stage-driven renewal for a time-driven lease. A real
Inventory Turn took more than 50 seconds, the lease expired during that Turn,
the following heartbeat was rejected, and the fifth attempt became
`retry_exhausted`.

The correction must make renewal independent of all processor and app-server
stage boundaries.

## 2. Boundaries

This slice will:

- keep one Capture Request lease alive from a successful claim until the
  request reaches a terminal mutation or processing is abandoned;
- run renewal independently while Capture or reconciliation is blocked;
- prevent local commit, upload, completion, or failure reporting after renewal
  has made ownership uncertain;
- use a dedicated HTTP client for renewal;
- stop and join the renewal worker deterministically; and
- preserve sanitized error codes and the existing central retry policy.

This slice will not:

- increase the lease duration to hide long model calls;
- move lease logic into the app-server transport;
- add cancellation of an in-flight Codex Turn;
- change the five-attempt policy;
- create or retry a real Capture Request during implementation; or
- refactor unrelated Agent, Candidate, Profile, or UI code.

## 3. Considered approaches

### 3.1 AgentService-owned Request Lease Session — selected

`AgentService` starts a request-scoped renewal session immediately after a
successful claim. A background worker renews every 10 seconds through a
dedicated client while the existing foreground processor runs unchanged.

This is the only option that covers the complete lease lifetime, including
future blocking work not implemented by the app-server layer.

### 3.2 Processor-owned renewal — rejected

Starting renewal inside `OnDemandCaptureProcessor` would be a smaller local
edit, but it would not cover the interval from claim to processor entry and
would split ownership of failure and terminal mutation across two layers.

### 3.3 Periodic callbacks inside app-server waiting — rejected

Waking `wait_for_notification` periodically would repair the observed model
wait, but it would couple central coordination to the Codex transport and leave
other long local or remote operations unprotected.

Increasing the lease duration alone is also rejected because any finite value
can be exceeded and does not repair the lifecycle model.

## 4. Components

### 4.1 Shared timing contract

The central API continues to grant a 30-second lease. The default renewal
interval is 10 seconds. These values live in one shared Capture Request lease
contract so the central API and local Agent cannot drift within one release.

Tests may inject a shorter interval, but production configuration cannot
disable renewal or choose an interval greater than one third of the lease.

### 4.2 RequestLeaseSession

A focused Agent module owns one request's renewal state:

- immutable request ID and lease token;
- a factory for a dedicated lease-only central client;
- one background worker;
- an interruptible stop event;
- the first sanitized renewal failure, if any; and
- `start`, `checkpoint`, and `quiesce` operations.

The background worker constructs and closes its own client on the same thread.
It never uses or closes the foreground client. It waits interruptibly for the
10-second interval, calls `heartbeat`, and repeats until stopped or until the
first renewal failure.

`start` includes a readiness handshake. It does not return until the worker has
constructed its client or recorded a sanitized construction failure. The
processor cannot begin during an unobserved client-startup race.

`checkpoint` raises the stored sanitized failure on the foreground thread.
The background thread never leaks raw HTTP exceptions and never mutates local
Capture state.

`quiesce` sets the stop event and joins the worker before returning. It then
surfaces any failure recorded while the worker was stopping. Once it returns
successfully, no later background heartbeat may be issued for that request.

### 4.3 Lease-aware foreground client

The processor receives a narrow facade around the existing foreground
`CentralClient`. Before `start`, `heartbeat`, `progress`, Candidate upload, or
completion, the facade calls the lease session's `checkpoint`.

Completion is a terminal boundary:

1. check renewal health;
2. quiesce and join the background worker;
3. check again for any failure recorded while the worker stopped;
4. send one final synchronous heartbeat to obtain a fresh lease window; and
5. call `complete` through the foreground client.

This prevents a background heartbeat from racing with a successful completion
and being misread as a lease failure after the request is already terminal.
If the final heartbeat or completion is rejected, the existing foreground
error path remains authoritative.

The existing explicit processor heartbeats remain as synchronous ownership
checks. One additional heartbeat occurs immediately before
`commit_candidate_result`, covering the empty-result and restored-result paths
before their durable local commit.

### 4.4 AgentService ownership

`AgentService.run_once` owns the full lifecycle:

```text
claim
  -> start RequestLeaseSession
  -> process through lease-aware foreground client
       -> terminal complete quiesces renewal first
  -> on processing error, quiesce renewal first
       -> if renewal stayed healthy, renew once synchronously and report fail
       -> if renewal failed, do not reuse the uncertain token
  -> return only after the renewal worker has stopped
```

The CLI composition root supplies the lease-client factory from the already
validated central URL and device token. Each claim gets a fresh lease-only
client with bounded short per-operation timeouts. Credentials are not read back
from another client's headers and are never logged.

## 5. Error semantics

The first background failure wins and is retained as a sanitized
`CentralClientError` code.

- Connection, network, and timeout failures become
  `central_connection_unavailable` or `central_temporarily_unavailable`.
- A rejected heartbeat means ownership is lost or uncertain.
- After any background renewal failure, the Agent performs no later guarded
  central mutation and does not call `fail` with the old token.
- The central service remains responsible for expiring and requeueing that
  request according to its existing attempt policy.
- If the processor itself fails while renewal is healthy, the Agent first
  quiesces renewal, sends one final synchronous heartbeat, and then uses the
  normal retryable or terminal `fail` path. If that heartbeat fails, it does
  not call `fail` with the uncertain token.
- A processor success is valid only after the central `complete` call returns
  successfully.

Expected HTTPX timeout and network subclasses at the CentralClient boundary
must be sanitized; exception text, URLs containing credentials, Session data,
Prompts, source code, and tool output never cross the boundary.

## 6. Crash and restart behavior

The renewal worker has no durable state. A process crash stops renewal and the
central lease expires. The existing central requeue policy and disposable local
Capture attempt recovery then apply.

No new local ownership record is required. The central lease token remains the
sole authority, and a restarted Agent must claim a new token before resuming.

## 7. Verification

The implementation uses TDD and adds only the following behavioral coverage:

1. While a fake processor is blocked, an independent heartbeat occurs and can
   release it without any processor callback.
2. A renewal failure blocks later guarded mutations and prevents a second
   `fail` using the uncertain token.
3. Both success and processor-error paths stop and join the renewal worker
   before `run_once` returns or reports failure.
4. A complete on-demand integration flow advances a controlled central clock
   beyond 30 seconds while Inventory remains blocked, observes multiple
   background renewals, then succeeds with `attempt_count == 1` and no
   `lease_expired_requeued` or `retry_exhausted` event.
5. Candidate result persistence has a synchronous lease checkpoint immediately
   before the durable commit.

Tests use `Event` or `Condition` coordination and a controlled clock. They do
not sleep for 30 seconds and do not depend on scheduler timing for correctness.

After focused tests pass, run the full suite once. Do not start another broad
review, Skill blind test, or real Capture Request. The already-terminal real
request remains immutable.

## 8. Acceptance criteria

The slice is complete when:

- a processor can remain inside one blocking structured Turn for longer than
  the 30-second lease while independent renewals continue;
- the request completes on its first claim in the controlled integration test;
- renewal and foreground HTTP clients have separate lifecycles;
- no heartbeat occurs after the terminal mutation boundary;
- background failure is observable and fail-closed;
- all focused and full tests pass; and
- no other product behavior or persistent schema changes.
