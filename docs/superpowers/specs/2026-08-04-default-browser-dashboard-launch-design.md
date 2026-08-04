# ZDecision Default-Browser Dashboard Launch Amendment

**Status:** Approved for implementation planning.

**Scope:** Replace the failed inline-card navigation experiment with a trusted
handoff from the ZDecision Plugin to the independent central decision system
in the operating system's default browser.

**Amends:** Section 9 and Gate 5 step 8 of
`2026-07-31-codex-inline-candidate-refresh-design.md`. All Capture, Candidate,
Review, publication, repository-binding, and card-freshness contracts remain
unchanged.

## 1. Product boundary

The central ZDecision system is an organization-level Web application, not a
Candidate-only extension of one Codex card. It will contain the Candidate
Inbox as well as product-scoped Decision boards, formal Decisions, publication
history, and shared views for authorized company members.

The inline Plugin card therefore remains a narrow development-workflow entry
point. It may start Candidate refresh, show bounded progress, and open the
central system. It must not absorb the central system's browsing, Review, or
publication interfaces.

After a successful refresh, including a successful zero-Candidate refresh,
the card displays **打开决策中心**. The resulting page is filtered to the
repository's mapped product using the central service's trusted repository
mapping. The current technical Demo may continue to use the existing
`repository_id` query route; future dashboard routes may change without
changing the card action contract.

## 2. Confirmed host limitation

Real Codex Desktop acceptance showed that an ordinary link inside the MCP Apps
sandbox creates an empty browser tab rather than navigating to the local
central page. The current host also rejects the local HTTP URL for
`ui/open-link`, and its supported external-link path does not promise the
Codex in-app Browser.

The Plugin must therefore stop using `ui/open-link`, `window.open`, or an HTML
anchor as the dashboard-launch mechanism. It also must not claim that a host
acknowledgement proves navigation.

A disposable real-host probe on 2026-08-04 then confirmed the selected
replacement boundary: one card click called an app-only local MCP tool, the
tool passed its locally derived dashboard URL to macOS, and the operating
system's default browser opened the central page. The probe accepted no URL
from the widget and was removed immediately after acceptance.

## 3. Selected architecture

```text
successful inline refresh card
  -> user clicks 打开决策中心
  -> app-only open_zdecision_dashboard(control_id)
  -> local MCP server validates the private Control Binding
  -> local MCP server derives the product-scoped central URL
  -> local Browser Launcher requests an OS default-browser tab
  -> independent central ZDecision Web application
```

The browser action is performed by the local ZDecision MCP process on the same
device as Codex. It does not require another model Turn and does not depend on
the Codex host choosing a browser surface.

The Browser Launcher is a small injected boundary. Production uses the
platform's default-browser facility with a platform API or a process argument
vector; it never interpolates the URL into a shell command. Tests use a
recording launcher and never open a real browser.

## 4. App-only tool contract

The local MCP server adds one app-visible, model-hidden action:

```text
open_zdecision_dashboard(control_id) -> {
  safe_state: launch_requested | unavailable,
  dashboard_url: string | null
}
```

The tool accepts no URL, repository ID, product ID, path, command, or browser
name from the widget. Before launching, it must:

1. resolve the opaque private Control Binding;
2. require a persisted selected scope and attached central request;
3. verify the local repository mapping still exists, is enabled, and matches
   the binding's repository and product;
4. validate the configured central base URL as HTTP or HTTPS with no embedded
   credentials, query, or fragment; and
5. derive the dashboard URL locally using the mapped repository identity.

The action is authorized only by the user's button click. Hook observation,
card rendering, polling, and terminal status alone never open a browser.

`launch_requested` means only that the operating-system launch request was
accepted by the local launcher. It does not prove that the browser rendered or
that the user authenticated successfully. `dashboard_url` is returned for
transparent fallback and must contain no credential or private Session data.

Because opening a browser tab is an observable local side effect, the tool is
annotated as non-read-only, non-destructive, non-idempotent, and open-world.
The widget never calls it without a direct user click.

## 5. Card behavior

The card resource receives the immutable host-visible URI
`ui://zdecision/update-candidates-v3.html` so that Codex does not reuse the
already-tested v2 snapshot after the launch behavior changes.

On terminal success:

- the button label is **打开决策中心**;
- the first click disables the button while one tool call is pending;
- a `launch_requested` response displays **已请求使用默认浏览器打开决策中心**;
- an `unavailable` response displays **无法自动打开，请使用下方地址** and
  exposes the exact selectable dashboard address when one is available; and
- the action remains retryable after the first call finishes.

The widget never automatically retries a launch request. If the response is
lost, the browser may already have opened, so the card displays an uncertain
result and lets the user explicitly retry. One explicit retry may open another
browser tab; it must never start another Capture Request or mutate Candidate
state.

The failed clickable-anchor probe is removed. A fallback address is selectable
text, not a second competing navigation mechanism.

## 6. Security and privacy

- The browser launcher receives only a locally derived HTTP or HTTPS URL.
- The widget cannot supply or alter the launch target.
- The launch tool is app-only and still performs server-side authorization.
- No device token, organization identity, Session ID, Turn ID, Prompt, source
  path, Candidate content, or Decision content appears in the tool input or
  dashboard URL.
- The central Web application remains responsible for browser-session
  authentication and authorization. The technical Demo credential model does
  not weaken the future company SSO boundary.
- Candidate, Review, and Decision data remain untrusted content and cannot
  instruct the local launcher.

## 7. Failure behavior

| Condition | Required result |
| --- | --- |
| Missing, expired, fabricated, or cross-product binding | `unavailable`; do not invoke the launcher. |
| No selected scope or no attached request | `unavailable`; do not invoke the launcher. |
| Repository mapping disabled or changed | `unavailable`; do not invoke the launcher. |
| Invalid central base URL | `unavailable`; do not invoke the launcher. |
| Launcher reports no accepted request | `unavailable`; expose the safe fallback address. |
| Tool response is lost after launch | Do not auto-retry or claim success; permit an explicit retry. |
| Central Web application is offline | The browser may show its ordinary connection failure; Capture state is unchanged. |

No launch failure may change the Capture Request, Candidate Inbox, Review
state, publication state, Control Binding scope, or Session checkpoint.

## 8. Acceptance gates

Automated acceptance must prove:

1. the widget calls only `open_zdecision_dashboard` and never sends a URL to
   the tool;
2. the tool rejects invalid bindings and mismatched or disabled mappings
   without invoking the launcher;
3. HTTP localhost and production HTTPS base URLs are both derived safely;
4. one valid click records one exact product-scoped URL in a fake launcher;
5. a rejected launch exposes a safe fallback without claiming navigation;
6. a lost response causes no automatic second launch; and
7. the card contains no Candidate or Decision payload.

One real Codex Desktop acceptance must then:

1. render a newly versioned current card in an enabled repository;
2. complete either refresh scope successfully;
3. click **打开决策中心** once;
4. observe the operating system's default browser open the exact repository-
   filtered central page;
5. verify Codex does not create an empty in-app Browser tab; and
6. verify the central page shows the current product context and synchronized
   Candidate revisions.

## 9. Implementation boundary and stopping rule

This amendment may change only:

- the card's resource URI, launch button, status copy, and fallback text;
- one app-only MCP tool and its testable local Browser Launcher boundary;
- the safe dashboard-URL helper and response envelope;
- focused widget, tool, and real Desktop acceptance tests; and
- the previously uncommitted failed anchor probe, which must be removed.

It must not redesign the central Web application, implement company SSO,
build Decision-board features, add another model Turn, patch Codex Desktop,
or introduce a second local Web server.

After implementation, run one focused test module, one complete test suite,
and one real default-browser acceptance. Fix only confirmed blockers from that
acceptance, record other improvements separately, and stop.
