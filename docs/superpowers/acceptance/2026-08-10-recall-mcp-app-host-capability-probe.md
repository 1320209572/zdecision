# Recall MCP App Host-Capability Probe

## Result

**PARTIAL**

The host accepted the app-only tool request and model-context update, and the
next manually submitted native message repeated the app-private marker without
a probe-read tool call.  Direct follow-up messaging is unavailable in this
host, so this is not a PASS.

Redacted marker prefix: `ZDECISION_HOST_PROBE_…`.  Receipt prefix:
`receipt_…`.

## Environment

- Codex Desktop: `26.803.41515` (build `6321`)
- Codex CLI: `0.147.0`
- Python: `3.14.4`; MCP SDK: `1.29.0`
- Git source commit: `d4679d35411dc09024ddc8816cf6ff139a1ffa17`
- Temporary Plugin manifest SHA-256: `33f648ffd95cbf8aeaa81058607cc2c20ab9594d02db81558b112092b158d2b7`
- Production `zdecision@zdecision-local` remained installed and enabled at
  `0.1.0+codex.20260810030551`.

## Capability and operation matrix

| Capability | Advertised | Actual request | Outcome |
| --- | --- | --- | --- |
| `serverTools` | yes | `tools/call` | pass |
| `updateModelContext.text` | yes | `ui/update-model-context` | pass |
| `message.text` | no | `ui/message` | unsupported |
| authoritative recovery | n/a | app-only `get` after remount | pass |

The host did not advertise `message.text`, and `ui/message` was unsupported.
The user manually sent the next native message from the same App View; that
message is evidence for the selected UX route, not a direct or
`host_confirmed` `ui/message` result.  On task switch and remount, the earlier
committed card restored its same receipt and showed `已恢复`, with no
cross-probe confusion.

## Safety evidence

- The selected successful trace made one mutating action call and produced one
  stable receipt.
- That trace had no automatic retry and made no new row during the reply turn.
- Procedural deviation: the user explicitly clicked two different cards,
  producing exactly two committed rows with distinct probe IDs and receipts.
  The report classifies only the selected successful trace.
- `git diff 19813a1 -- plugins/zdecision` produced no output: no production
  ZDecision Plugin change.
- No App Server, transcript, or business-data access occurred.
- Privacy scan passed: this report contains only capability categories, a
  redacted marker prefix, and a receipt prefix; it contains no raw tool result,
  private database row, task text, full marker, or full receipt.

## Route selected

- PARTIAL message -> next-native-message UX
