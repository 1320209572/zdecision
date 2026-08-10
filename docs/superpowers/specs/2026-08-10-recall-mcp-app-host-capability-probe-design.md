# Recall MCP App Host-Capability Probe Design

**Status:** Proposed for written-spec approval on 2026-08-10

**Scope:** Prove the current Codex Desktop MCP Apps handoff needed by Recall
without changing production Recall behavior or touching real Decisions.

## 1. Decision context

The current Recall confirmation card now proves the user's consent correctly:
the trusted render path creates one private activation attempt, the app-only
button action commits that attempt, and the card can recover the committed
state. The remaining failure happens after consent. The implementation asks a
later model tool to re-prove the active Turn by starting another App Server and
requiring a persisted `hookPrompt`. The current Desktop does not expose that
fact through the supported Plugin/MCP contract, so the Turn gate fails even
after a valid user click.

That failed proof is not repaired by another parser, retry, or inferred Prompt
marker. Before replacing the handoff, ZDecision will run one isolated
capability probe against the exact installed Codex Desktop host.

The probe answers four bounded questions:

1. Can an inline MCP App call an app-only server tool through `tools/call`?
2. Can that tool own an idempotent authoritative result that survives card
   remount?
3. Can the card place one bounded value into model-visible context through
   `ui/update-model-context`?
4. Can the card request a follow-up Turn through `ui/message`, including a
   host-native send confirmation if the host requires one?

The probe does not infer that an advertised capability works. It records the
advertised capability and then exercises the corresponding operation.

## 2. Authority and amendments

This document temporarily amends only the post-confirmation handoff and
verification assumptions in:

- sections 3.1, 5.3, 8, 9, and 10 of
  `2026-08-09-recall-user-confirmation-entry-design.md`; and
- the current-Recall boundary in section 12.4 of `docs/architecture.md`.

The 2026-08-09 design remains authoritative for the app-only consent action,
trusted activation attempt, repository validation, no-consent failure path,
Session lifetime, Candidate isolation, and privacy boundary.

This probe is a newly authorized exception to that design's earlier statement
that no replacement feasibility gate was needed. The exception is narrow: a
real accepted click subsequently proved that the production handoff depends on
an undocumented and unavailable App Server read-back path. The native
Elicitation gate stays rejected and is not rerun.

The probe does not approve the final Recall handoff. A passing result selects
the next design route; a failing result selects a bounded fallback route. A
separate implementation plan is still required after this specification is
approved.

## 3. Official and open-source basis

The probe follows the standards-first MCP Apps flow documented by OpenAI:

- associate an inline UI resource with a render tool;
- initialize through `ui/initialize`;
- call an MCP tool from the UI through `tools/call`;
- update model-visible UI context through `ui/update-model-context`;
- request a follow-up through `ui/message`; and
- feature-detect the exact capability instead of branching on a host name.

OpenAI's UI guidance also fixes the state boundary used here: authoritative
business state belongs on the MCP server, the UI calls a tool to mutate it,
the server returns the authoritative snapshot, and the UI renders that
snapshot. An inline card is the recommended presentation for focused
confirmation or a small action set.

Reference implementations reviewed before this decision include the OpenAI
role-specific Codex plugins, OpenAI Apps SDK examples, the MCP Apps reference
implementation and examples, and current open-source Codex plugins. Their
shared pattern is card action -> server-owned result -> model-context update ->
follow-up message. None uses a second App Server plus `thread/read` and
`hookPrompt` as the authorization or delivery proof for an MCP App action.

The fixed source evidence is:

- OpenAI's UI guide defines `tools/call`, `ui/update-model-context`,
  `ui/message`, capability detection, inline cards, and server-owned state:
  <https://developers.openai.com/plugins/build/chatgpt-ui>.
- OpenAI's MIT Apps SDK examples provide an app-only server tool, actual
  `updateModelContext`, actual `sendMessage`, and host-capability inspection:
  [app-only tools](https://github.com/openai/openai-apps-sdk-examples/blob/18cc38e78a968712c357bacdc3c79fead5bfc6b4/mcp_app_basics_node/src/server.ts#L423-L550),
  [context update](https://github.com/openai/openai-apps-sdk-examples/blob/18cc38e78a968712c357bacdc3c79fead5bfc6b4/src/update-model-context/App.tsx#L62-L78),
  [message](https://github.com/openai/openai-apps-sdk-examples/blob/18cc38e78a968712c357bacdc3c79fead5bfc6b4/src/send-message/App.tsx#L62-L84), and
  [capabilities](https://github.com/openai/openai-apps-sdk-examples/blob/18cc38e78a968712c357bacdc3c79fead5bfc6b4/src/get-host-capabilities/App.tsx#L18-L44).
- OpenAI's MIT Data Analytics Codex Plugin feature-detects `message.text` and
  prefers its native follow-up helper before the standard fallback:
  [host bridge](https://github.com/openai/role-specific-plugins/blob/fe5608d2512a7d6a7b9821ce8a88c48464ecd6e4/plugins/data-analytics/src/mcp-host.js#L105-L164) and
  [follow-up](https://github.com/openai/role-specific-plugins/blob/fe5608d2512a7d6a7b9821ce8a88c48464ecd6e4/plugins/data-analytics/src/analytics-app/App.tsx#L2987-L3007).
- The Apache/MIT MCP Apps reference requires one aggregated context update
  before a short message and documents stable view recovery:
  [handoff order](https://github.com/modelcontextprotocol/ext-apps/blob/92f46a574568a3ddac7600343b7d3c4c4ed7b588/docs/patterns.md#L480-L504) and
  [remount pattern](https://github.com/modelcontextprotocol/ext-apps/blob/92f46a574568a3ddac7600343b7d3c4c4ed7b588/docs/patterns.md#L506-L573).
  Its stable types define the exact capability and request shapes used by the
  probe: [protocol types](https://github.com/modelcontextprotocol/ext-apps/blob/92f46a574568a3ddac7600343b7d3c4c4ed7b588/src/spec.types.ts#L424-L532).
- Esse is a real MIT Codex Plugin with app-only tools, the same three bridge
  operations, and request-key/fingerprint recovery:
  [tool visibility](https://github.com/renoir1220/esse/blob/a100665a74bc8e47925de1ff35c1cff06ec4e274/plugins/codex/src/mcp/app.ts#L463-L593),
  [bridge](https://github.com/renoir1220/esse/blob/a100665a74bc8e47925de1ff35c1cff06ec4e274/plugins/codex/web/bridge.ts#L28-L76), and
  [idempotent recovery](https://github.com/renoir1220/esse/blob/a100665a74bc8e47925de1ff35c1cff06ec4e274/plugins/codex/src/jobs/batch-manager.ts#L82-L153).

The official Codex Security Plugin was also inspected as a behavioral
reference for claim/release/delivered recovery, but its manifest is
proprietary. No Codex Security source may be copied into ZDecision. Likewise,
private Desktop IPC patterns from experimental plugins are explicitly outside
this design.

## 4. Probe boundaries

### 4.1 In scope

The probe is packaged as a separate temporary local Plugin with the fixed
identity `zdecision-host-probe`. It is not added to the production
`plugins/zdecision` manifest, Skill tree, Hook tree, MCP server map, or frozen
Recall bundle. It uses its own MCP process and private state file so installing
it cannot invalidate a pending confirmation, change a production bundle
digest, contend on the production Recall store, or request another Hook trust
decision.

The temporary Plugin adds one diagnostic resource and exactly three diagnostic
tools:

- `show_zdecision_recall_host_probe`: model-visible render tool with no input;
- `run_zdecision_recall_host_probe`: app-only idempotent action tool; and
- `get_zdecision_recall_host_probe`: app-only read-only recovery tool.

The resource URI is exactly:

`ui://zdecision/recall-host-capability-probe-v1.html`

The probe uses a dedicated private store outside the production ZDecision
database. It does not reuse an activation attempt, Recall Session, Turn gate,
Candidate control binding, or formal Decision identifier.

### 4.2 Out of scope

The probe must not:

- read or write a formal Decision, Candidate, Review, Registry, or Central
  record;
- run product routing, hybrid retrieval, embeddings, reranking, applicability,
  or context-compaction restoration;
- read Prompt text, transcript, rollout, source, diff, tool output, PRD, or
  task summary;
- call App Server, `thread/read`, `thread/items/list`, or transcript paths;
- add or change a Hook, Hook trust prompt, repository registration, or
  Candidate behavior;
- create a Recall Session, Intent Epoch, active injected set, or bypass;
- infer a user click from model text or Skill selection;
- become a permanent end-user Plugin capability; or
- modify the installed production ZDecision Plugin or its cache entry.

The probe runs in a dedicated acceptance task. It does not require an enabled
repository because repository eligibility is already covered by the production
confirmation tests and is deliberately outside this host-transport check.

## 5. Probe protocol

### 5.1 Render

The model or operator calls `show_zdecision_recall_host_probe` once. The server:

1. creates a random, single-use `probe_id` and a separate random bounded
   `marker`;
2. stores one private `ready` probe record;
3. returns only `state = ready` as model-visible structured content; and
4. returns the `probe_id` only in app-private tool-result `_meta`.

The marker is non-secret test data, but it is not included in the render
tool's model-visible result. The model must learn it only if the later
model-context update succeeds.

The card does nothing on load except initialize and render the server snapshot.
It never runs the action on load, restoration, retry, or remount.

### 5.2 Capability observation

After `ui/initialize`, the card records the exact host capability object for
the three operations under test:

- `hostCapabilities.serverTools` for app-to-server tool calls;
- `hostCapabilities.updateModelContext.text` for text model-context updates;
  and
- `hostCapabilities.message.text` for text follow-up messages.

`serverTools` is an object whose presence authorizes the Host proxy for
`tools/call`; its optional `listChanged` member is unrelated to this probe.
`updateModelContext` and `message` are modality objects, not booleans, so text
support exists only when their `text` member is present.

Unknown, missing, or malformed capability fields are recorded as unsupported.
The card does not branch on `Codex`, `ChatGPT`, application version, or product
name. Capability advertisement is diagnostic only; an actual successful method
call is required for a passing observation.

### 5.3 User action and authoritative server state

The card has one button: **运行宿主能力验证**. On one click it calls
`run_zdecision_recall_host_probe` with only the app-private `probe_id`.

The app-only action atomically changes `ready -> committed`. Its
model-compatible structured result returns the bounded state and receipt:

```text
probe_version
state = committed
receipt
committed_at
```

The marker is returned only in app-private tool-result `_meta`. It must not be
present in `structuredContent`, text content, the conversation tool result, or
the follow-up message. This keeps `ui/update-model-context` as the only route by
which the model can learn the marker.

The first committed receipt wins. Duplicate, concurrent, or replayed calls for
the same `probe_id` return the same snapshot and never create a new marker or
receipt. Unknown or expired IDs fail closed. A tool timeout is an unknown
client result, not permission to repeat the mutation automatically.

The private store contains only those bounded diagnostic fields and expiry
metadata. Records expire within 24 hours and may be removed after the final
acceptance report.

### 5.4 Model-context handoff

After receiving a valid committed snapshot, the card calls
`ui/update-model-context` exactly once with one text content block containing:

- the bounded marker;
- the instruction to repeat that marker in the next response; and
- the statement that no tool may be called to rediscover the marker.

The card must not call the method when the corresponding capability is absent.
It must not send multiple partial updates: the complete probe context is one
atomic update so a last-write-wins host cannot retain only a fragment.

If the method rejects, times out, or returns a malformed response, the card
records `context_update = failed`, does not claim that context was injected,
and does not retry automatically.

`ui/update-model-context` is sent as a JSON-RPC request with an ID, not as a
fire-and-forget notification. The card awaits its successful empty result
before it may request a follow-up. The update does not itself start a new Turn,
and the host may defer the context until the next user message.

### 5.5 Follow-up request

Only after a successful model-context update does the card call `ui/message`
once with a bounded request to return the probe marker. The message itself does
not contain the marker.

`ui/message` is also a JSON-RPC request. Its payload is exactly one
`role = user` message containing one text content block. An `isError = true`
result is a failed observation even if the transport request itself completed.

The observation is classified as one of:

- `direct`: the host starts the follow-up without another user gesture;
- `host_confirmed`: the host presents its native send-confirmation surface and
  starts the follow-up after the user confirms;
- `unsupported`: the capability is absent;
- `failed`: the method rejects, times out, or produces no usable follow-up.

`host_confirmed` is a supported host behavior, not a transport failure. It is
recorded because it adds a visible product interaction that the final Recall
design must either accept or avoid.

The follow-up response passes the context check only if it contains the exact
marker without calling a ZDecision tool that could read the marker from the
private probe store.

### 5.6 Remount and unknown-result recovery

On card remount, the UI may call the app-only read-only
`get_zdecision_recall_host_probe` once with the same app-private `probe_id`.
It renders the authoritative snapshot and never calls the mutating action,
`ui/update-model-context`, or `ui/message` automatically.

Automated tests simulate a lost action response followed by remount. The
recovery tool must return the already committed receipt. Real Desktop
acceptance also switches away from and back to the task after commitment and
must show the same committed state.

## 6. Private state and privacy

The diagnostic record lives in the temporary Plugin's own owner-readable
SQLite file and is isolated from production Recall tables and business
objects. Its exact logical fields are:

```text
probe_id
probe_version
state = ready | committed | failed | expired
marker
receipt
created_at
committed_at
expires_at
```

`probe_id`, `marker`, and `receipt` are cryptographically random bounded
values. They identify no user, Session, repository, product, Prompt, or
Decision. The card receives `probe_id` only in `_meta`; the model-visible tool
result and follow-up request do not expose it.

No probe value is sent to Central or written to Git. The acceptance report may
record the host version, Plugin bundle digest, boolean capability observations,
method outcomes, and a redacted marker prefix. It must not record raw task
content or private store rows.

## 7. Automated acceptance

Focused tests must prove:

1. only the render tool is model-visible; action and recovery tools are
   app-only;
2. the render schema has no input and exposes the `probe_id` only through
   `_meta`;
3. load and remount never call the mutating action or send context/messages;
4. one click makes exactly one `tools/call` with the app-private ID;
5. duplicate/concurrent action calls return the same committed receipt;
6. a lost action response is recovered by the read-only status tool;
7. malformed, expired, unknown, or mismatched IDs cannot commit or read a
   record;
8. capability absence and malformed capability objects produce bounded
   unsupported outcomes;
9. call order is exactly `tools/call -> ui/update-model-context -> ui/message`;
10. failed or timed-out context/message operations are not retried
    automatically;
11. the complete model context is sent in one update; and
12. privacy sentinels are absent from the store, resource result, logs, and
    report fixture.

Existing Candidate and Recall confirmation source, installed bundle, behavior,
and focused suites must remain unchanged and green. Their exact resource/tool
inventory must not contain the diagnostic entries because the probe has a
separate Plugin and MCP namespace. The probe must not require changes to the
current Hook matcher or Hook latency contract.

## 8. Real Codex Desktop acceptance

Run one bounded acceptance on the exact installed Desktop build:

1. install the temporary `zdecision-host-probe` Plugin and restart Codex once;
2. in a dedicated task, invoke the diagnostic render tool;
3. record the capability object as bounded booleans/categories;
4. click **运行宿主能力验证** once;
5. if Codex presents its native follow-up confirmation, click **发送** once;
6. verify the next assistant response contains the exact marker without a
   ZDecision marker-read tool call;
7. switch to another task and back, then verify the card restores the same
   committed receipt; and
8. record one PASS/PARTIAL/FAIL report and stop.

The acceptance categories are:

- **PASS:** app-only `tools/call`, server idempotency/recovery, actual
  `ui/update-model-context`, and actual `ui/message` all work; message delivery
  is additionally classified as `direct` or `host_confirmed`.
- **PARTIAL:** app-only action and recovery work, but either model-context
  update or follow-up messaging is unsupported or fails.
- **FAIL:** the app-only tool is callable by the model, the card cannot call it,
  the server cannot recover authoritative state, or any cross-probe/state
  confusion occurs.

Do not repeat a real case merely to obtain a preferred result. A single
transport failure may be classified with one read-only recovery check; it does
not authorize another mutation.

## 9. Route selected by the result

### 9.1 PASS

Write a narrow production amendment that keeps the trusted app-only consent
action, moves typed Recall Intent and product routing into the pre-click frozen
attempt, and changes the button action into one server-owned
`enable-and-recall` transaction. The action returns the validated published
Decision set, the card performs one model-context update, and it then requests
the bounded follow-up.

The production amendment removes `gate_zdecision_turn`, the second App Server,
`thread/read`, `hookPrompt` proof, and active-Turn item-order proof from the
Recall hot path. It retains server-side idempotency, receipts, freshness,
active injected set, context epochs, and explicit bypass.

### 9.2 PARTIAL: context update unavailable

Do not emulate `ui/update-model-context` and do not restore App Server
read-back. Keep the app-only server action and design one next-native-Turn
handoff through the official `UserPromptSubmit.additionalContext` output,
bound to the already trusted Session and committed Recall state. That fallback
requires its own short specification and real acceptance before production.

### 9.3 PARTIAL: follow-up message unavailable

Recall remains technically feasible if app-only action, authoritative recovery,
and model-context update pass. The production card states that Recall is ready
and takes effect on the next native user message. No synthetic Turn or retry is
created.

### 9.4 FAIL

Stop Plugin-based Recall implementation for this host. Do not add Prompt
parsing, transcript parsing, hidden automatic confirmation, destructive tool
annotations, or another App Server process. A stronger product would require a
separate first-party App Server client that owns `turn/start` and the event
stream; that is a new architecture decision, not a repair to this Plugin.

## 10. Stop and cleanup rules

This design authorizes exactly one isolated host probe and one acceptance
report. It does not authorize production Recall changes.

After the report:

- freeze the observed host version and outcomes;
- uninstall the temporary Plugin and remove its marketplace/cache entry after
  the report; the production ZDecision bundle is not reinstalled for cleanup;
- delete expired diagnostic records;
- do not rerun broad architecture review; and
- write only the result-selected production amendment and implementation plan.

If implementation discovers that the current Desktop cannot expose the needed
capability object, cannot distinguish app-only tools from model-visible tools,
or cannot perform the calls without reading business data, stop and report the
exact gap instead of expanding the probe.
