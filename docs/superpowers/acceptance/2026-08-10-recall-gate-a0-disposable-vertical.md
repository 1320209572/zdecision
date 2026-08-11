# Recall Gate A0 Disposable Vertical Acceptance

**Date:** 2026-08-11
**Verdict:** PASS
**Scope:** Disposable, test-only next-native-message Recall handoff. This is not production Decision retrieval.

## Environment

- Codex Desktop: `26.803.41515` (`6321`)
- Desktop-bundled Codex CLI: `0.147.0-alpha.6.5`
- Shell Codex CLI: `0.147.0`
- Python: `3.14.4`
- MCP SDK: `1.29.0`
- ZDecision commit: `75bb315`
- Test location: one registered and enabled Git repository

## Behavior matrix

| Check | Result | Bounded evidence |
|---|---|---|
| Disposable Plugin discovery | PASS | The unique selector was installed and enabled; the production ZDecision Plugin remained installed and enabled. |
| Hook trust preflight | PASS | The exact generated `PreToolUse` source was present in the selected Codex configuration before interaction. No trust hash or configuration value was retained. |
| Trusted render binding | PASS | One native task produced exactly one trusted Attempt; the model supplied no accepted host binding identifier. |
| App-only confirmation | PASS | One explicit user click produced one Delivery and one server-authoritative receipt. |
| Context handoff | PASS | One acknowledged context update delivered the typed intent and two canonical test Decisions; the next native message used them without shell, search, file-read, status, or render tools. |
| Remount recovery | PASS | Leaving the task and returning retained the delivered card/attachment without a second Delivery or context update. |
| Semantic filtering | PASS | Classifications were exactly `applicable` and `not_applicable`; exactly one fixture became active. |
| Pre-application guard | PASS | The first counter attempt was denied before application and produced no mutation. |
| Atomic application | PASS | One Application was committed with two nonempty bounded reasons and no duplicate Application row. |
| Post-application guard | PASS | One mutation claim was consumed once and the disposable counter became `1`. |
| Ordinary follow-up reuse | PASS | A later native message completed with zero tool calls and left every Delivery, Application, and mutation count unchanged. |
| Isolation | PASS | No production Candidate, Registry, Central, or production Plugin state was changed. No second App Server path was used. |
| Cleanup | PASS | The disposable selector and marketplace were removed, all exact disposable MCP leases exited, and only the recorded disposable root was deleted. |

## Final bounded state

```text
attempts=1
deliveries=1
context_updates=1
classifications=[applicable, not_applicable]
applications=1
active_fixtures=1
pre_application_denials=1
mutation_claims=1
mutation_counter=1
followup_tool_calls=0
duplicate_groups=0
sqlite_integrity=ok
foreign_key_errors=0
```

Digest and receipt prefixes retained for correlation only:

```text
fixture_digests=[e6400d33b97e, 64145131255f]
snapshot_digest=23206d3a0938
delivery_receipt=delivery_receipt_3d7...
application_receipt=application_receipt_ade...
```

## Protocol evidence

- The card acknowledged exactly one successful `ui/update-model-context` handoff in disposable state.
- The delivered typed intent and both Decisions were consumed in the next native message without any local read/search tool.
- No automatic follow-up Turn occurred; the user explicitly sent the next native message, so the selected `next-native-message` route remained in effect and `ui/message` was not used.
- The subsequent ordinary message performed no tool call, retrieval, reinjection, application, or mutation.

The host did not retain a separate raw UI-bridge method trace for this view. The verdict therefore uses the server acknowledgment, the single persisted context-update count, the next-message consumption result, and the absence of an automatic Turn as the end-to-end protocol evidence.

## Bounded procedural deviation

Before the successful Application call, the model made one incomplete request containing only the delivered identifier and no classifications. `PreToolUse` rejected it before creating a binding or invoking the MCP server. The model then issued one complete request in the same native Turn. The rejected request caused no row, receipt, context update, Application, or mutation and was not an ambiguous side-effecting retry.

## Boundary

This PASS proves the disposable client-side handoff and application boundary only:

1. explicit user confirmation can create one authoritative Delivery;
2. `ui/update-model-context` can make a frozen typed snapshot available to the next native message;
3. Codex can semantically select the applicable Decision without local transcript or file reads;
4. trusted Hooks can deny covered mutation until application and permit it afterward; and
5. repeated native messages do not automatically redeliver or reapply the same state.

The provider and Decisions were synthetic and test-only. Production Recall, trusted distribution, retrieval quality, and context-compaction restoration remain unavailable until their later gates are implemented and accepted.
