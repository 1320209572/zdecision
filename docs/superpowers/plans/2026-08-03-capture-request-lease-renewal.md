# Capture Request Lease Renewal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every claimed Capture Request lease alive independently of blocking app-server work, and fail closed if renewal becomes uncertain.

**Architecture:** `AgentService` owns a request-scoped `RequestLeaseSession` immediately after claim. A dedicated background `CentralClient` renews the 30-second lease every 10 seconds, while a lease-aware facade guards foreground mutations and quiesces renewal before `complete` or `fail`.

**Tech Stack:** Python 3.14, standard-library `threading`, HTTPX, FastAPI/TestClient, SQLite, `unittest`.

## Global Constraints

- Keep the central lease at exactly 30 seconds and the production renewal interval at exactly 10 seconds.
- Use a separate HTTP client for renewal; never share or close the foreground client from the renewal worker.
- Begin renewal after claim and before processor entry.
- Stop and join renewal before terminal `complete` or `fail`.
- After renewal failure, do not commit, upload, complete, or call `fail` with the uncertain token.
- Keep existing synchronous processor heartbeats and add one immediately before Candidate result commit.
- Do not change Candidate extraction, model profiles, Review, publication, inline controls, persistent schemas, or the five-attempt policy.
- Do not create or retry a real Capture Request during implementation.
- Use condition-based tests; no 30-second sleeps.
- Stop after one focused suite and one full suite; do not start a broad review loop.

---

### Task 1: Request lease primitive and guarded foreground client

**Files:**
- Create: `src/zdecision/agent/request_lease.py`
- Create: `tests/test_request_lease.py`
- Modify: `src/zdecision/sync/contracts.py`
- Modify: `src/zdecision/central/api.py`
- Modify: `src/zdecision/agent/central_client.py`
- Modify: `tests/test_central_client.py`

**Interfaces:**
- Produces: `CAPTURE_REQUEST_LEASE_SECONDS: int = 30` and `CAPTURE_REQUEST_RENEW_INTERVAL_SECONDS: float = 10.0`.
- Produces: `RequestLeaseSession(request_id, lease_token, client_factory, interval_seconds=10.0)` with `start()`, `checkpoint()`, `mark_uncertain(error)`, `quiesce()`, and `uncertain`.
- Produces: `LeaseAwareCentralClient(foreground, lease_session)` implementing `start`, `heartbeat`, `progress`, `upload_candidates`, and `complete`.
- Consumes: any lease-only client with `heartbeat(request_id, lease_token)` and `close()`.

- [ ] **Step 1: Write failing lease-session lifecycle tests**

Create `tests/test_request_lease.py` with event-driven fakes. Removing the background worker must leave the processor-side wait blocked.

```python
from __future__ import annotations

import threading
import unittest

from zdecision.agent.central_client import CentralClientError
from zdecision.agent.request_lease import (
    LeaseAwareCentralClient,
    RequestLeaseSession,
)

REQUEST_ID = "crq_" + "1" * 32
LEASE_TOKEN = "lease_0123456789abcdef"


class LeaseClient:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.heartbeat_seen = threading.Event()
        self.closed = threading.Event()
        self.count = 0

    def heartbeat(self, request_id: str, lease_token: str) -> None:
        self.count += 1
        self.heartbeat_seen.set()
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed.set()


class RequestLeaseSessionTest(unittest.TestCase):
    def test_renews_while_foreground_is_blocked_and_quiesces(self) -> None:
        lease_client = LeaseClient()
        session = RequestLeaseSession(
            REQUEST_ID,
            LEASE_TOKEN,
            client_factory=lambda: lease_client,
            interval_seconds=0.001,
        )

        session.start()
        self.assertTrue(lease_client.heartbeat_seen.wait(timeout=1.0))
        session.quiesce()

        self.assertGreaterEqual(lease_client.count, 1)
        self.assertTrue(lease_client.closed.is_set())

    def test_first_renewal_failure_is_rethrown_on_foreground(self) -> None:
        lease_client = LeaseClient(
            failure=CentralClientError("central_request_rejected")
        )
        session = RequestLeaseSession(
            REQUEST_ID,
            LEASE_TOKEN,
            client_factory=lambda: lease_client,
            interval_seconds=0.001,
        )

        session.start()
        self.assertTrue(lease_client.heartbeat_seen.wait(timeout=1.0))
        with self.assertRaisesRegex(
            CentralClientError, "central_request_rejected"
        ):
            session.checkpoint()
        self.assertTrue(session.uncertain)
        with self.assertRaises(CentralClientError):
            session.quiesce()
```

- [ ] **Step 2: Run the new tests and verify RED**

```bash
./.venv/bin/python -m unittest tests.test_request_lease -v
```

Expected: import failure because `zdecision.agent.request_lease` does not exist.

- [ ] **Step 3: Add the shared timing contract and make the central API consume it**

Add to `src/zdecision/sync/contracts.py`:

```python
CAPTURE_REQUEST_LEASE_SECONDS = 30
CAPTURE_REQUEST_RENEW_INTERVAL_SECONDS = 10.0
```

Import `CAPTURE_REQUEST_LEASE_SECONDS` in `src/zdecision/central/api.py` and replace both literal `lease_seconds=30` arguments used by claim and heartbeat. Do not change the API payload or response schema.

- [ ] **Step 4: Implement `RequestLeaseSession` minimally**

Create `src/zdecision/agent/request_lease.py` with these synchronization rules:

```python
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from zdecision.agent.central_client import CentralClientError
from zdecision.sync.contracts import (
    CAPTURE_REQUEST_LEASE_SECONDS,
    CAPTURE_REQUEST_RENEW_INTERVAL_SECONDS,
)


class LeaseHeartbeatClient(Protocol):
    def heartbeat(self, request_id: str, lease_token: str) -> None: ...
    def close(self) -> None: ...


class RequestLeaseSession:
    def __init__(
        self,
        request_id: str,
        lease_token: str,
        *,
        client_factory: Callable[[], LeaseHeartbeatClient],
        interval_seconds: float = CAPTURE_REQUEST_RENEW_INTERVAL_SECONDS,
    ) -> None:
        if (
            not isinstance(interval_seconds, (int, float))
            or isinstance(interval_seconds, bool)
            or not 0 < interval_seconds <= CAPTURE_REQUEST_LEASE_SECONDS / 3
        ):
            raise ValueError("lease renewal interval is invalid")
        self.request_id = request_id
        self.lease_token = lease_token
        self.client_factory = client_factory
        self.interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._failure_code: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def uncertain(self) -> bool:
        with self._lock:
            return self._failure_code is not None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("lease session already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"zdecision-lease-{self.request_id[-8:]}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()
        self.checkpoint()

    def checkpoint(self) -> None:
        with self._lock:
            code = self._failure_code
        if code is not None:
            raise CentralClientError(code)

    def mark_uncertain(self, error: Exception) -> None:
        code = (
            error.code
            if isinstance(error, CentralClientError)
            else "central_connection_unavailable"
        )
        with self._lock:
            if self._failure_code is None:
                self._failure_code = code
        self._stop.set()

    def quiesce(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self.checkpoint()

    def _run(self) -> None:
        client: LeaseHeartbeatClient | None = None
        try:
            try:
                client = self.client_factory()
            except Exception as error:
                self.mark_uncertain(error)
            finally:
                self._ready.set()
            if client is None:
                return
            while not self._stop.wait(self.interval_seconds):
                try:
                    client.heartbeat(self.request_id, self.lease_token)
                except Exception as error:
                    self.mark_uncertain(error)
                    return
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            self._ready.set()
```

Keep client creation and closure on the worker thread. `quiesce()` must be idempotent and must not return before the thread exits.

- [ ] **Step 5: Add the guarded completion test and verify RED**

Add a fake foreground client whose `heartbeat` asserts that the lease client is already closed. Then call `LeaseAwareCentralClient.complete` and assert the exact foreground order is `heartbeat`, `complete`.

```python
    def test_complete_quiesces_then_renews_once_before_terminal_call(self) -> None:
        lease_client = LeaseClient()
        session = RequestLeaseSession(
            REQUEST_ID,
            LEASE_TOKEN,
            client_factory=lambda: lease_client,
            interval_seconds=10.0,
        )
        session.start()
        calls: list[str] = []

        class Foreground:
            def heartbeat(self, request_id: str, lease_token: str) -> None:
                if not lease_client.closed.is_set():
                    raise AssertionError("renewal worker was not quiesced")
                calls.append("heartbeat")

            def complete(
                self, request_id: str, lease_token: str, batch_digest: str
            ) -> None:
                calls.append("complete")

        guarded = LeaseAwareCentralClient(Foreground(), session)
        guarded.complete(REQUEST_ID, LEASE_TOKEN, "a" * 64)
        self.assertEqual(["heartbeat", "complete"], calls)
```

Expected RED: `LeaseAwareCentralClient` is missing.

- [ ] **Step 6: Implement `LeaseAwareCentralClient`**

Implement explicit forwarding methods. Every method calls `checkpoint()` before delegation. `heartbeat` marks the session uncertain when its foreground call fails. `complete` performs `checkpoint`, `quiesce`, a final synchronous heartbeat, then completion:

```python
class LeaseAwareCentralClient:
    def __init__(self, foreground, lease_session: RequestLeaseSession) -> None:
        self._foreground = foreground
        self._lease_session = lease_session

    def heartbeat(self, request_id: str, lease_token: str) -> None:
        self._lease_session.checkpoint()
        try:
            self._foreground.heartbeat(request_id, lease_token)
        except Exception as error:
            self._lease_session.mark_uncertain(error)
            raise

    def start(self, request_id: str, lease_token: str) -> None:
        self._lease_session.checkpoint()
        self._foreground.start(request_id, lease_token)

    def progress(self, request_id: str, lease_token: str, code: str) -> None:
        self._lease_session.checkpoint()
        self._foreground.progress(request_id, lease_token, code)

    def upload_candidates(self, lease_token: str, batch):
        self._lease_session.checkpoint()
        return self._foreground.upload_candidates(lease_token, batch)

    def complete(
        self, request_id: str, lease_token: str, batch_digest: str
    ) -> None:
        self._lease_session.checkpoint()
        self._lease_session.quiesce()
        try:
            self._foreground.heartbeat(request_id, lease_token)
        except Exception as error:
            self._lease_session.mark_uncertain(error)
            raise
        self._foreground.complete(request_id, lease_token, batch_digest)
```

- [ ] **Step 7: Add and implement HTTPX transport sanitization**

In `tests/test_central_client.py`, make a transport raise `httpx.ReadTimeout` and assert that one request raises only `CentralClientError("central_connection_unavailable")` without retry. Add this branch after the existing connect-error retry branch in `CentralClient._request`:

```python
            except httpx.TransportError as error:
                raise CentralClientError(
                    "central_connection_unavailable"
                ) from error
```

Do not retry ambiguous read, write, or protocol failures because a POST may already have reached the server.

- [ ] **Step 8: Run focused Task 1 tests**

```bash
./.venv/bin/python -m unittest \
  tests.test_request_lease \
  tests.test_central_client -v
```

Expected: all Task 1 tests pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add \
  src/zdecision/agent/request_lease.py \
  src/zdecision/agent/central_client.py \
  src/zdecision/central/api.py \
  src/zdecision/sync/contracts.py \
  tests/test_request_lease.py \
  tests/test_central_client.py
git commit -m "fix: add independent capture request lease session"
```

---

### Task 2: AgentService lifecycle and production client factory

**Files:**
- Modify: `src/zdecision/agent/service.py`
- Modify: `src/zdecision/agent/cli.py`
- Modify: `tests/test_agent_service.py`

**Interfaces:**
- Consumes: `RequestLeaseSession` and `LeaseAwareCentralClient` from Task 1.
- Produces: `AgentService(..., lease_client_factory, lease_interval_seconds=10.0)`.
- Preserves: `run_once() -> bool` and existing retryable and terminal processor error codes.

- [ ] **Step 1: Extend AgentService test fakes without changing behavior**

Add `threading` and a separate fake lease client. Extend the foreground fake with `heartbeat`, `progress`, ordered calls, and existing `fail`. Pass `lease_client_factory` to every existing `AgentService` construction.

```python
class FakeLeaseClient:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.heartbeat_seen = threading.Event()
        self.closed = threading.Event()

    def heartbeat(self, request_id: str, lease_token: str) -> None:
        self.heartbeat_seen.set()
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed.set()
```

- [ ] **Step 2: Write the blocked-processor renewal test and verify RED**

```python
    def test_service_renews_lease_while_processor_is_blocked(self) -> None:
        lease_client = FakeLeaseClient()

        class BlockingProcessor:
            def process(self, request, client) -> None:
                if not lease_client.heartbeat_seen.wait(timeout=1.0):
                    raise AssertionError("independent renewal did not run")

        service = AgentService(
            client=FakeCentralClient([claimed_request()]),
            processor=BlockingProcessor(),
            lease_client_factory=lambda: lease_client,
            lease_interval_seconds=0.001,
            sleeper=lambda _: None,
        )

        self.assertTrue(service.run_once())
        self.assertTrue(lease_client.closed.is_set())
```

Expected RED: `AgentService` does not accept the lease factory and no independent heartbeat exists.

- [ ] **Step 3: Write failure and stop-order tests and verify RED**

Add these observable behaviors:

1. A rejecting renewal client sets its event; the processor then calls guarded `progress`; the underlying foreground `progress` and `fail` lists remain empty.
2. For a normal `RetryableCaptureRequestError`, the foreground order is final `heartbeat` followed by `fail`, and `fail` observes that the lease client is already closed.
3. For a successful fake processor return, `run_once` does not return until the lease client is closed.

The renewal-rejection assertions are:

```python
self.assertEqual([], client.progresses)
self.assertEqual([], client.failures)
self.assertTrue(lease_client.closed.is_set())
```

- [ ] **Step 4: Integrate the lease lifecycle into `AgentService.run_once`**

Add required constructor input `lease_client_factory` and optional test interval. After claim, construct and start one session before processor entry, and pass a guarded foreground client. Represent processor failures as `(code, retryable)` locally.

```python
        session = RequestLeaseSession(
            request.request_id,
            request.lease_token,
            client_factory=self.lease_client_factory,
            interval_seconds=self.lease_interval_seconds,
        )
        failure: tuple[str, bool] | None = None
        try:
            session.start()
            guarded = LeaseAwareCentralClient(self.client, session)
            self.processor.process(request, guarded)
        except RetryableCaptureRequestError as error:
            failure = (error.code, True)
        except TerminalCaptureRequestError as error:
            failure = (error.code, False)
        except Exception:
            failure = ("unexpected_processor_error", True)
        finally:
            try:
                session.quiesce()
            except CentralClientError:
                pass

        if failure is not None and not session.uncertain:
            try:
                self.client.heartbeat(request.request_id, request.lease_token)
            except Exception as error:
                session.mark_uncertain(error)
            if not session.uncertain:
                self.client.fail(
                    request.request_id,
                    request.lease_token,
                    failure[0],
                    retryable=failure[1],
                )
```

Do not start the session when `processor is None` or `claim_next()` returns `None`.

- [ ] **Step 5: Wire a separate production client factory in the CLI**

Keep the existing foreground client. Pass a lambda that captures the validated URL and token and constructs a new `CentralClient` only after a claim. Give the lease-only client shorter HTTPX timeouts:

```python
import httpx

lease_timeout = httpx.Timeout(
    5.0,
    connect=3.0,
    write=5.0,
    pool=3.0,
)
AgentService(
    client=client,
    processor=configured_processor(database, config, state_path),
    lease_client_factory=lambda: CentralClient(
        config.central_url,
        config.device_token,
        timeout=lease_timeout,
    ),
).run_forever()
```

The renewal worker closes only clients returned by this factory. The CLI's existing `finally` continues to close the foreground client.

- [ ] **Step 6: Run focused Task 2 tests**

```bash
./.venv/bin/python -m unittest tests.test_agent_service -v
```

Expected: all AgentService and CLI lifecycle tests pass with no live renewal thread after each test.

- [ ] **Step 7: Commit Task 2**

```bash
git add \
  src/zdecision/agent/service.py \
  src/zdecision/agent/cli.py \
  tests/test_agent_service.py
git commit -m "fix: renew claimed requests throughout processing"
```

---

### Task 3: Pre-commit checkpoint and long-block integration regression

**Files:**
- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `tests/test_capture_request_processor.py`
- Modify: `tests/integration/test_on_demand_capture_core.py`

**Interfaces:**
- Consumes: `AgentService` lease factory and guarded client from Tasks 1–2.
- Produces: one synchronous `heartbeat` immediately before `commit_candidate_result`.
- Proves: a single claim survives more than 30 logical seconds inside Inventory.

- [ ] **Step 1: Write the pre-commit checkpoint test and verify RED**

In `tests/test_capture_request_processor.py`, process a request with no observed Session sources. Wrap `commit_candidate_result` and assert that the last central call is `heartbeat` before delegating to the real method:

```python
    def test_empty_result_checks_lease_immediately_before_local_commit(self) -> None:
        original = self.request_state.commit_candidate_result

        def checked_commit(request_id, result, batch):
            self.assertEqual("heartbeat", self.client.calls[-1])
            return original(request_id, result, batch)

        with patch.object(
            self.request_state,
            "commit_candidate_result",
            side_effect=checked_commit,
        ):
            self.processor.process(claimed_request(), self.client)
```

Run the single test. Expected RED: the last call is `start`, proving the empty-source path lacks a lease checkpoint.

- [ ] **Step 2: Add the minimal processor heartbeat**

In `OnDemandCaptureProcessor._process`, after verifying `result.repository_id` and before constructing or committing the Candidate batch, call:

```python
        client.heartbeat(request.request_id, request.lease_token)
```

Do not move or remove the existing stage heartbeats.

- [ ] **Step 3: Make the integration clock and bridge thread-coordinated**

In `tests/integration/test_on_demand_capture_core.py`:

- protect `MutableClock.value` with `threading.Lock`;
- give `TestClientBridge` a `threading.Condition`;
- append request records while holding that condition and call `notify_all()`;
- add `heartbeat_count(request_id)` and `wait_for_heartbeat(request_id, after_count, timeout)` helpers that inspect only paths ending in `/{request_id}/heartbeat`.

The wait helper must use `Condition.wait_for`, not `time.sleep`.

- [ ] **Step 4: Add a controllable Inventory blocking hook**

Add `self.before_inventory_result: Callable[[], None] | None = None` to `FakeAppServerGateway`. In `run_structured_turn`, after identifying the Inventory stage and before creating its receipt, call it once when present:

```python
        if stage == "inventory" and self.before_inventory_result is not None:
            callback = self.before_inventory_result
            self.before_inventory_result = None
            callback()
```

- [ ] **Step 5: Wire a dedicated integration lease client**

In `_start_local`, construct `AgentService` with:

```python
        self.agent_service = AgentService(
            client=self.central_client,
            processor=processor,
            lease_client_factory=lambda: CentralClient(
                "http://central.test",
                DEVICE_TOKEN,
                transport=httpx.MockTransport(self.bridge),
                sleeper=lambda _: None,
            ),
            lease_interval_seconds=0.001,
        )
```

Each factory call must construct a fresh `MockTransport` and `CentralClient`.

- [ ] **Step 6: Write the controlled long-block integration test and verify RED**

```python
    def test_blocking_inventory_keeps_one_live_lease_past_thirty_seconds(
        self,
    ) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_long_inventory")

        def cross_lease_window() -> None:
            for _ in range(4):
                self.clock.advance(9)
                before = self.bridge.heartbeat_count(request_id)
                self.assertTrue(
                    self.bridge.wait_for_heartbeat(
                        request_id, after_count=before, timeout=1.0
                    )
                )

        self.gateway.before_inventory_result = cross_lease_window

        self.assertTrue(self._run_agent_once())

        request = self._request(request_id)
        event_codes = [
            item["code"]
            for item in self.browser.get(
                f"/api/v1/capture-requests/{request_id}/events"
            ).json()["events"]
        ]
        record = self.central_store.get_request_record(request_id)
        self.assertEqual("succeeded", request["state"])
        self.assertEqual(1, record.attempt_count)
        self.assertNotIn("lease_expired_requeued", event_codes)
        self.assertNotIn("retry_exhausted", event_codes)
```

Before Tasks 1–2 this test reaches its one-second safety timeout because no independent heartbeat can occur while Inventory is blocked. After implementation it must pass without 30 seconds of wall-clock waiting.

- [ ] **Step 7: Run the focused Gate C regression suite**

```bash
./.venv/bin/python -m unittest \
  tests.test_request_lease \
  tests.test_agent_service \
  tests.test_central_client \
  tests.test_capture_request_processor \
  tests.integration.test_on_demand_capture_core -v
```

Expected: all focused tests pass and no test leaves a lease thread or open client.

- [ ] **Step 8: Commit Task 3**

```bash
git add \
  src/zdecision/agent/capture_processor.py \
  tests/test_capture_request_processor.py \
  tests/integration/test_on_demand_capture_core.py
git commit -m "test: cover capture lease during blocking model work"
```

---

### Task 4: Bounded final verification

**Files:**
- Modify only files required by a confirmed focused or full-suite regression.

**Interfaces:**
- Consumes: the complete lease-renewal slice from Tasks 1–3.
- Produces: one clean focused run, one clean full run, and a clean worktree.

- [ ] **Step 1: Run formatting and focused verification once**

```bash
git diff --check
./.venv/bin/python -m unittest \
  tests.test_request_lease \
  tests.test_agent_service \
  tests.test_central_client \
  tests.test_capture_request_processor \
  tests.integration.test_on_demand_capture_core
```

Expected: exit status 0.

- [ ] **Step 2: Run the complete suite once**

```bash
./.venv/bin/python -m unittest discover -s tests
```

Expected: exit status 0. Existing explicitly documented skips remain allowed; no new skip is added.

- [ ] **Step 3: Inspect final repository state**

```bash
git status --short --branch
git log -5 --oneline
```

Expected: clean `main`, ahead of `origin/main`, with the three implementation commits. Do not push and do not create a real Capture Request without a new explicit user instruction.
