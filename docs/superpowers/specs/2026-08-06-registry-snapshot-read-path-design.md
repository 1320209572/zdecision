# Registry Snapshot Read Path Design

**Status:** Proposed for final user review.

**Scope:** Remove remote Git synchronization and full Registry reconstruction
from ordinary Central Web reads. Preserve every existing publication,
recovery, exact-main, and Registry ownership invariant.

## 1. Problem and evidence

The Central Web currently constructs a new formal Registry snapshot inside
each Dashboard, Decision catalog, Decision detail, and repository-space
request. Snapshot construction executes `git fetch origin main` before and
after reading the Registry so that the request can prove the commit did not
change mid-read.

That safety check is correct at a mutation boundary but disproportionate on a
read path:

- one fetch takes about 3.9 seconds in the current environment;
- Dashboard takes about 8 seconds and formal Decisions about 10.4 seconds;
- Candidate and Publication list APIs take about 7 milliseconds in isolation;
  and
- a concurrent Dashboard request delays the 7-millisecond Candidate request
  to about 9.56 seconds because synchronous Git subprocesses block the single
  Uvicorn event loop.

The response bodies are small. JavaScript size, CSS, React rendering, and the
current amount of decision data are not the primary cause.

## 2. Goals

- Dashboard and formal Decision reads perform no network Git operation.
- Ordinary reads return one immutable, previously verified Registry snapshot.
- A completed publication becomes visible to subsequent reads immediately,
  without restarting the Central service.
- Preview creation, publication confirmation, publication recovery, and push
  retain their current fresh-fetch and exact-main checks.
- Candidate and Publication APIs must not wait behind read-only Registry
  snapshot construction triggered by Dashboard or Decision navigation.
- The change remains in-process and V1-sized; it introduces no Redis, queue,
  webhook, distributed lease, or second Registry writer.

## 3. Considered approaches

### A. Event-driven immutable snapshot — selected

Build and verify a Registry snapshot once from the local last-known exact
`main` state when Central starts. Store that immutable snapshot in memory.
Ordinary page requests only read the stored value. After Central itself
completes or resumes a publication, rebuild and atomically replace the
snapshot from the completed publication commit.

Central remains the only Registry writer in V1. A fresh remote fetch remains
mandatory before preview creation and at publication/recovery boundaries, but
not for browsing a commit that has already been verified.

This removes network latency from the hot path without weakening the write
path or creating concurrent background Git mutations.

### B. TTL cache inside requests

Cache a snapshot for a short interval and let the first request after expiry
refresh it. This reduces average latency but still makes one user pay the full
cost, permits duplicate refreshes, and can continue blocking unrelated APIs.

### C. Thread offload only

Run current Git work in a worker thread. This stops head-of-line blocking but
Dashboard and Decision pages still take 8–10 seconds and still perform two
remote fetches per navigation.

Approach A is selected. Thread offload and frontend caching are not substitutes
for removing unnecessary work from the request path.

## 4. Registry snapshot contract

`RegistryQuery` gains two distinct responsibilities with explicit methods:

- `refresh_local(expected_commit: str | None = None) -> RegistrySnapshot`
  verifies that local `HEAD`, `refs/heads/main`, and the last-known
  `refs/remotes/origin/main` are identical, reads and validates the complete
  Registry at that commit, verifies the local refs are still identical, and
  atomically installs the resulting immutable snapshot. It performs no
  network fetch.
- `snapshot() -> RegistrySnapshot` returns the installed immutable snapshot
  without Git or filesystem work. If none has been installed, it raises the
  existing `RegistryQueryUnavailable` error.

The existing canonical JSON, product ownership, Decision ownership, head
revision, and lifecycle validation remain unchanged inside snapshot building.
Only when and where building runs changes.

`GitRegistryAdapter` exposes a local-only exact-main check rather than asking
callers to reach into private revision helpers. The existing
`fetch_and_require_exact_main()` method remains unchanged for preview and
publication correctness.

The installed snapshot is replaced only after the new snapshot has been fully
read and validated. Readers therefore see either the complete previous commit
or the complete new commit, never a partially built Registry.

## 5. Startup and publication flow

### 5.1 Central startup

Before Uvicorn accepts requests, the CLI calls `refresh_local()` once. This is
local Git work only. If the local exact-main proof or Registry validation
fails, Central may continue serving Candidate and Publication operational
data, but formal Registry views remain explicitly unavailable; they do not
fall back to empty results and do not initiate a remote fetch from a page
request.

The current LaunchAgent and CLI do not gain a periodic background fetch in
this slice. Central is the only supported V1 Registry writer, so startup and
successful publication are the only events that can install a new formal
snapshot. External manual pushes remain outside the V1 writer contract and
are detected by the next existing fresh publication check.

### 5.2 Successful publish or resume

Publication confirmation and recovery run their current exact sequence:
fresh fetch, exact-main validation, exact commit creation/reconciliation,
push, and remote proof.

Only after the durable Publication record reports `completed`, the application
calls `refresh_local(publication.commit_sha)`. That local rebuild publishes
the newly completed Decision to readers immediately and performs no additional
network fetch.

If post-publication snapshot rebuilding unexpectedly fails, the completed
Publication remains durable and must not be rolled back. The in-memory formal
snapshot is invalidated so readers receive Registry unavailable instead of a
silently stale catalog; restarting Central retries the local rebuild.

## 6. HTTP and frontend behavior

The existing HTTP response schemas and routes remain unchanged. Dashboard,
Decision list/detail, and repository-space queries continue to receive a
`RegistrySnapshot`; they simply receive it from memory.

The Dashboard copy changes from **Registry 已同步** to **Registry 已验证**.
The displayed commit remains the proof of which immutable snapshot is shown.

No general frontend query cache is introduced in this slice. Once the backend
hot path is local and nonblocking, page requests are already millisecond-scale.
Adding frontend cache invalidation before it is needed would risk hiding a
newly published Decision and would expand the change beyond the diagnosed
root cause.

## 7. Concurrency and trust boundaries

- Page requests never acquire a Git synchronization lock or start a subprocess.
- Snapshot replacement is thread-safe and atomic.
- Candidate, Review, Preview, Publication, and Decision content remains
  untrusted data and cannot trigger refresh behavior.
- Only Central startup and a native completed publication/recovery event may
  install a snapshot.
- The formal publication boundary continues to require fresh remote state;
  the read cache never authorizes a commit or push.

## 8. Verification and acceptance

Automated tests must prove:

1. `snapshot()` returns a previously installed object without invoking Git.
2. repeated Dashboard and Decision queries do not fetch or reconstruct the
   Registry;
3. an unavailable startup snapshot stays unavailable without a request-time
   fetch;
4. `refresh_local()` rejects mismatched local refs and never replaces a valid
   snapshot with a partially built one;
5. completed publish and resume install the exact completed commit;
6. preview and publication tests still perform fresh exact-main checks; and
7. existing Registry corruption, stale preview, ambiguous publication, and
   crash-recovery tests remain green.

Real acceptance on the running demo must prove:

- Dashboard and formal Decisions return without any `git fetch` in their
  request path;
- repeated warm requests complete below 200 milliseconds on the current
  machine;
- a concurrent Dashboard request does not delay Candidate beyond 200
  milliseconds; and
- the Decision published in the previous acceptance remains visible with
  Registry commit `02ae3c37e388b74004d771d18468bba06f90a1f6`.

## 9. Stop rule

Stop after the verified snapshot read path, startup installation, completed
publication refresh, semantic Dashboard copy, focused tests, one full test
run, and one real latency check pass. Do not add distributed cache, remote
webhook, polling scheduler, SQL N+1 cleanup, generic React data library, or a
new review cycle in this slice.
