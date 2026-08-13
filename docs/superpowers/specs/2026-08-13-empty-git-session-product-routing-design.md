# Empty-Git Session Product Routing Design

**Status:** Approved in conversation on 2026-08-13

## Goal

Allow Candidate refresh to continue when the frozen Git path evidence is
empty by letting the configured Capture model select one registered leaf
route from the frozen Session conversation. Existing Git path routing remains
authoritative whenever any Git path evidence exists.

## Scope

This is a local Agent fallback. It does not add UI, GitLab/MR access, user
product selection, a Central schema, or a second routing service.

The fallback runs only when `FrozenGitPathEvidence.paths` is exactly empty.
Non-empty evidence that matches no registered route keeps the existing
`no_routable_decision_space_changes` outcome; the model cannot override it.

## Routing flow

1. The Agent freezes the selected Session sources and Git evidence exactly as
   it does today.
2. If Git evidence contains paths, the existing deterministic path matcher
   creates the Capture plan without invoking model routing.
3. If Git evidence contains no paths, the Agent processes each frozen Session
   source independently:
   - verify that the exact completed source boundary is still available;
   - fork that exact boundary into an internal read-only Capture thread;
   - ask the already configured and request-frozen Capture model to select
     exactly one enabled route from the server-issued route snapshot;
   - require a closed structured result containing one `route_id` from the
     supplied enum.
4. The Agent groups sources by selected route and creates the normal leaf
   Capture slices. A slice contains only the source keys classified to that
   route and has an empty matched-path set.
5. The normal Inventory, Extraction, reconciliation, upload, Review, and
   publication flow continues unchanged.

The prompt supplies only registered route IDs and their configured path
prefixes. It instructs the model to judge from the inherited Session
conversation and not to call tools. The model cannot create a route, select a
disabled route, or return a Decision-space identity directly.

## Frozen and replayed state

`CaptureGroupPlan` records the routing method (`git_paths` or
`session_model`). For model routing it additionally records, per frozen source,
the selected route ID and the structured-output digest. The existing
`CaptureRoutingStore` writes the complete plan once under the Capture request
ID. Restarts and retries load that immutable plan and do not invoke the model
again, even if the Session or repository later changes.

The source-boundary digest, route-snapshot digest, model profile, plan digest,
and existing corruption/conflict checks remain mandatory. Nothing from the
raw Session is written to the routing database or sent to Central.

## Failure behavior

- Missing, incomplete, non-interactive, or wrong-CWD source boundaries fail
  closed before Candidate extraction.
- A failed structured model turn is retryable while no route plan exists.
- Malformed output, an unregistered route ID, mixed model profiles, or a
  conflicting persisted plan fails closed.
- There is no confidence threshold and no follow-up product question. A valid
  registered route chosen by the model is accepted by design.

## Authority amendment

This design narrows the existing rule that model text cannot choose Candidate
ownership. The revised rule is:

- trusted non-empty Git path evidence always chooses ownership;
- only when the frozen Git path set is empty may the configured Capture model
  choose one registered enabled leaf route from the exact frozen Session
  boundary;
- Candidate text, browser input, upload payloads, arbitrary model-provided
  identities, and non-empty unmatched Git evidence still cannot choose or
  override ownership.

## Verification

Automated tests must prove:

- non-empty Git evidence never invokes the model fallback;
- empty Git evidence routes a Session mentioning `third-party-services` to
  the registered Third-party Services route;
- all-valid scope can group independently classified sources into separate
  slices;
- only each slice's selected sources reach its Capture runner;
- a persisted semantic plan replays without another model call;
- malformed or out-of-snapshot output fails closed;
- no raw Session content crosses the local routing-plan or Central boundary;
- existing path-routing and empty-unmatched behavior remain compatible.
