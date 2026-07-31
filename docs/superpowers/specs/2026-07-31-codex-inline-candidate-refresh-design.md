# ZDecision Codex Inline Candidate Refresh Design

**Status:** Approved for implementation planning.

**Scope:** Replace the proven no-side-effect Codex UI probe with the first
real, user-authorized Candidate refresh control.

**Amends:** `2026-07-30-on-demand-candidate-refresh-design.md`. That document
remains authoritative for repository observation, two-stage Capture,
Candidate reconciliation, central Review, publication, and later Decision
recall. This design changes only the user entry point, source-scope selection,
and inline progress presentation.

## 1. Proven host capability

The minimal MCP Apps probe was accepted in Codex Desktop on 2026-07-31:

```text
Codex renders the ZDecision card
  -> user clicks 更新候选决策
  -> widget calls an app-visible MCP tool
  -> local MCP server returns the acknowledgement
  -> widget renders the successful acknowledgement
```

This proves the current Codex Desktop host can render the Plugin's versioned
`ui://` resource and execute a widget-originated `tools/call`. The probe did
not create a Capture Request or change Candidate state.

The implementation may therefore use an inline MCP Apps card as a product
surface. The central page remains the only Candidate content, Review, and
publication surface.

## 2. Product decisions

The inline card is context-sensitive to the Codex task in which it is shown.
It never offers a cross-repository picker.

The card contains exactly two Capture actions:

- **当前 Session**
- **所有有效 Session**

`当前 Session` selects only the trusted current Codex Session. `所有有效
Session` selects every changed, eligible interactive Session in the current
repository. "All" does not mean reprocessing unchanged Sessions: a Session
whose acknowledged checkpoint and source fingerprint have not changed is
excluded. Subagent Sessions remain excluded.

Both actions immediately create a durable Capture Request. There is no second
confirmation because the request creates only reviewable Candidates. It never
accepts, publishes, or mutates a formal Decision.

If the current task cannot be bound to an organization-registered, enabled
Git repository, the card still renders but both actions are disabled. It does
not display the reason.

The card displays safe request progress and the number of Candidate revisions
synchronized by that request. It does not display Candidate content. On
success it offers an entry to the central Candidate page, where later Review
and publication occur.

### 2.1 Card presentation timing

An MCP Apps card is a conversation result, not a permanent Codex toolbar
control. The Plugin Skill therefore instructs Codex to render the card once
after it completes and verifies a normal code-development boundary in an
enabled repository.

Rendering the card is not Capture authorization and performs no model-based
extraction. The user must still choose one of its two actions. If proactive
rendering does not occur, the user may say **更新候选决策** in the same Codex
task and the Skill renders the card immediately.

The Plugin does not render the card at Session start, after every Turn, or for
non-code work in this slice. Duplicate render attempts are harmless and do not
create a Capture Request.

## 3. Rejected source-selection approaches

### 3.1 Guess the most recently active Session

Rejected. Two Codex tasks may be active in the same repository. Recency is not
identity and can select the wrong source.

### 3.2 Accept a Session ID from the model or widget

Rejected. Model and widget arguments are untrusted inputs. Raw Session
identity also must not cross the central-service boundary.

### 3.3 Trusted Hook binding

Selected. Codex `PreToolUse` Hooks receive host-owned `session_id`, `turn_id`,
and `cwd` for MCP tool calls and may replace the MCP input before execution.
ZDecision uses that capability only to establish a bounded local control
binding. It does not use a Hook to authorize or start Candidate generation.
The user's later click remains the only Capture authority.

## 4. Components and trust boundaries

```text
Codex task
  -> model-visible render tool
  -> PreToolUse Hook receives trusted task identity
  -> private local Control Binding
  -> inline card with two actions
  -> app-only start/status tools
  -> authenticated central Capture Request
  -> persistent local Agent claims request
  -> existing two-stage Capture and reconciliation
  -> structured Candidate batch
  -> central Candidate Inbox
```

The responsibilities are:

- **Codex host:** supplies native Session, Turn, and working-directory facts to
  the Hook.
- **PreToolUse Hook:** resolves the repository, creates the local binding, and
  rewrites the render-tool input with one opaque control ID.
- **MCP render tool:** reads the binding and returns authoritative card state.
- **MCP action tools:** validate the control, create or read one central
  request, and return only safe status data.
- **Local Agent:** selects the bound local source set and runs the existing
  Capture pipeline.
- **Central service:** owns request identity, concurrency, progress, Candidate
  synchronization, and the review-page route.

The MCP process and persistent local Agent use the same owner-readable Agent
configuration selected during onboarding. A validated owner-readable runtime
locator contains its absolute path; no token is added to MCP arguments, the
locator, or Plugin manifests. The existing raw device token remains only in
the referenced Agent configuration.

## 5. Trusted Control Binding

### 5.1 Creation

The Plugin adds a `PreToolUse` matcher scoped only to the ZDecision card render
tool. For a matching call, the Hook:

1. validates the host's `session_id`, `turn_id`, and absolute `cwd`;
2. resolves `cwd` to the normalized Git repository identity;
3. verifies the local repository mapping is present and enabled;
4. creates a cryptographically random `control_id`;
5. stores the private binding in the local Agent database; and
6. returns `permissionDecision: allow` with render-tool `updatedInput`
   containing only the `control_id`.

The binding contains:

```text
control_id
session_id
render_turn_id
cwd
repository_id
product_id
created_at
expires_at
chosen_scope             nullable
client_action_id         nullable
central_request_id       nullable
```

It contains no Prompt, transcript path, tool input, source code, diff, or
Candidate content. A control expires 15 minutes after creation if no action
has been selected. Rendering another card creates another control.

If any validation or repository check fails, the Hook does not create a
binding. The render tool accepts the missing binding and returns a disabled
card without a reason string.

### 5.2 Scope selection and replay

The widget calls an app-only action tool with:

```text
control_id
scope: current_session | all_valid_sessions
```

The first valid action atomically records `chosen_scope`, generates one
independent stable `client_action_id`, and persists both values. The action ID
is generated separately from the control ID and never exposes the control ID
to the central service. After that:

- the same control and same scope replay the same operation;
- the same control with another scope returns a bounded conflict;
- both buttons become disabled as soon as one scope is selected; and
- the control-to-request association remains readable after the initial
  15-minute selection window so that the mounted card can follow its durable
  request to a terminal state.

The action tool is visible to the MCP App but not to the model. Tool visibility
is a host affordance, not the server's only security check: the server still
requires a valid unexpired local binding for the first scope selection and
enforces the one-scope rule. An already selected control may replay the same
action and follow its durable request after the initial selection expiry.

## 6. Capture Request contracts

### 6.1 Safe central fields

The inline action creates a central request containing only:

```text
repository_id
template_id
capture_scope
client_action_id
```

`capture_scope` is exactly `current_session` or `all_valid_sessions`. The
central request, events, logs, and claim response never contain a Session ID,
Turn ID, working directory, Prompt, source path, code, diff, or tool output.

The existing page action explicitly sends
`capture_scope: all_valid_sessions`; it preserves its current repository-wide
behavior.

The central claim response adds `capture_scope` and `client_action_id`. These
safe fields allow the local Agent to locate an already-persisted private
intent. A `current_session` request with no matching private intent fails
closed with a bounded terminal code; it never falls back to another Session or
to repository-wide Capture.

### 6.2 Identity

For the technical Demo, the inline action uses a dedicated Plugin action
endpoint authenticated with the existing device credential. The central
identity adapter first authenticates the registered device, then derives the
configured Demo user and organization server-side. The client cannot submit
organization, actor, product, or device identity.

The production OIDC/OAuth adapter will replace this Demo authentication edge
without changing the service-level `Principal` or Capture Request contracts.
The technical Demo does not claim that possession of the local device
credential is equivalent to company-email SSO.

### 6.3 Durable ordering

The MCP action persists the chosen local intent before its first network call.
The central request uses the stable `client_action_id`. Therefore:

- a crash before the network call leaves a replayable local intent;
- a lost central response is retried with the same action ID;
- a replay returns the same request;
- the Agent can resolve the private intent directly from the
  `client_action_id` in the claim; and
- no crash window can make a current-Session request silently become an
  all-Session request.

## 7. Local source freezing

The existing request freeze becomes scope-aware.

For `current_session`, the Agent:

1. loads the private intent by `client_action_id`;
2. verifies its repository and chosen scope match the central claim;
3. finds that bound Session's latest indexed durable completed checkpoint;
4. freezes it only when its checkpoint or source fingerprint differs from the
   last acknowledged values; and
5. returns an empty source set when there is no new handled boundary.

For `all_valid_sessions`, the Agent uses the existing repository-wide query:
all changed, non-excluded interactive Session checkpoints for that repository.

The frozen source set remains immutable for the request. Activity after the
freeze waits for a later click. The existing acknowledgement rule remains:
handled checkpoints advance only after the central service acknowledges the
complete Candidate batch, including a valid zero-Candidate result.

No extraction, reconciliation, Candidate-family, outbox, or acknowledgement
algorithm is replaced by this design.

## 8. Repository concurrency

The first inline version keeps one active Capture Request per repository and
does not add request queues.

- An exact `client_action_id` replay returns the original request.
- A different action while the repository has an active request returns
  `repository_capture_busy`.
- A card receiving that result displays **已有更新正在进行** and creates no
  request.
- The user may click again after the active request reaches a terminal state.

The service must not attach a second card to an unrelated active request. That
would misrepresent which current Session was selected.

## 9. Inline UI state

The card owns only ephemeral presentation state. Central request state remains
authoritative.

Before selection:

- show **当前 Session** and **所有有效 Session**;
- enable both only when a trusted Control Binding exists; and
- show no repository-disable explanation.

After selection:

- disable both actions;
- show the central request lifecycle using bounded labels;
- poll through an app-only status tool rather than accessing Candidate data;
  and
- stop polling at a terminal state.

Safe user-facing states are:

```text
正在创建更新请求
等待本地设备
正在整理候选决策
正在同步候选决策
已有更新正在进行
暂时无法更新
本次更新未完成
没有发现新的候选决策
本次同步 N 条候选决策
```

`N` is the number of Candidate revisions in this request's acknowledged
upload batch, not the total number of Candidates currently in the product.

On terminal success, the card shows **打开候选决策页面** and uses the
host-supported external-navigation method to open the repository's central
Candidate page. It does not receive or render Candidate content itself.

## 10. Failure and recovery

| Condition | Required behavior |
| --- | --- |
| Missing or disabled repository mapping | Render both actions disabled without a reason. |
| Hook missing, rejected, or unable to persist a binding | Render both actions disabled without a reason. |
| Expired unused control | Reject the action and require a newly rendered card. |
| Same control and same scope replay | Return or resume the same local intent and central request. |
| Same control with another scope | Return a bounded conflict and create no request. |
| Central unavailable before request creation | Keep the local intent replayable and show a generic temporary failure. |
| Central response lost after creation | Retry the same action ID and adopt the original request. |
| Local Agent offline | Preserve the queued central request and show waiting-for-device state. |
| Current-Session intent missing or mismatched | Fail terminal; never guess another source. |
| Another action already active for the repository | Return busy; do not reuse or queue behind it. |
| Capture or reconciliation failure | Preserve existing retry/terminal semantics and show only a generic failure in the card. |
| Zero changed sources or zero Candidate revisions | Complete normally as `succeeded_no_candidates`. |

Ordinary Codex work remains independent of every failure above.

## 11. Security and privacy

- Only the user's card click selects a Capture scope.
- The model-visible render tool remains read-only and cannot start Capture.
- The mutating action is app-only, idempotent for one chosen scope, and still
  validates a local Control Binding.
- The Hook uses host-owned identity only to create that binding. Hook
  observation alone never creates a Candidate.
- Organization, actor, and product are derived from server-side identity and
  repository mappings.
- Session and Turn identifiers never leave the device.
- Candidate content reaches the central Candidate Inbox only through the
  existing validated batch contract.
- Candidate content remains untrusted review material and cannot instruct the
  Plugin, Agent, model, or publication service.
- Review and publication remain separate explicit user operations.

## 12. Acceptance gates

### Gate 1: Trusted Codex binding

- An enabled repository renders two usable actions.
- An unregistered, disabled, or unresolved repository renders both actions
  disabled with no explanation.
- Completing and verifying a normal code task renders the card once without
  starting Capture.
- Saying **更新候选决策** in the same task renders the card as a deterministic
  fallback.
- Session start, ordinary intermediate Turns, and non-code work do not
  proactively render the card.
- Two concurrent Codex tasks in the same repository receive distinct local
  bindings.
- Each task's **当前 Session** action resolves its own native Session, never
  the most recently active Session.
- A fabricated, expired, or cross-repository control ID is rejected.

### Gate 2: Scope semantics

- **当前 Session** freezes only the bound Session's latest changed checkpoint.
- Another changed Session remains unhandled for a later request.
- **所有有效 Session** freezes all changed eligible interactive Sessions.
- Acknowledged unchanged Sessions and subagent Sessions are excluded.
- Activity arriving after either frozen boundary waits for the next action.

### Gate 3: Idempotency and concurrency

- Repeated clicks and a lost create response yield one request.
- MCP restart after local intent persistence resumes the same request.
- Same-control scope conflict creates no second request.
- Another card while the repository is active receives busy.
- After terminal completion, a new action may create the next request.

### Gate 4: Progress and privacy

- The card follows queued, running, and terminal state without Candidate
  content.
- Success displays the acknowledged batch revision count.
- Zero revisions displays a successful empty result.
- Central request records, events, application logs, and database values
  contain no Session ID, Turn ID, Prompt, source code, diff, tool output, local
  path, or control ID.

### Gate 5: Real Codex Desktop acceptance

1. Open two normal Codex tasks in one enabled repository and create distinct
   changed checkpoints.
2. Render the ZDecision card in one task.
3. Click **当前 Session**.
4. Observe one request progress to a terminal state and display its revision
   count.
5. Verify only that task's Session checkpoint advanced.
6. Render another card and click **所有有效 Session**.
7. Verify the remaining changed Session is handled without reprocessing the
   unchanged first Session.
8. Open the central page and see the corresponding current Candidate
   revisions.

The Gate fails if the user must supply a Session ID, the wrong Session is
selected, a second action silently attaches to an unrelated request, Candidate
content appears in the card, or any raw local source identity reaches central
storage.

## 13. Implementation boundary and stopping rule

This slice may change only:

- the ZDecision Skill instruction that presents the card after a verified code
  boundary or an explicit same-task request;
- the inline MCP Apps resource and render/action/status tools;
- the narrow `PreToolUse` binding Hook;
- private Control Binding and local request-intent state;
- Capture Request scope/action transport fields;
- scope-aware Session freezing;
- safe terminal Candidate count; and
- the existing page's explicit all-valid scope.

It must not implement Candidate Review, publication, Decision recall, company
OIDC/SSO, request queues, scheduled Capture, non-code sources, or production
visual design.

After implementation, run one focused suite, one complete suite, and one real
Codex Desktop acceptance. A confirmed blocking defect permits one focused
correction. Record other improvements for later work and stop; do not begin a
new broad architecture audit.
