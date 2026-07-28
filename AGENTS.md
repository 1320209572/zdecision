# ZDecision repository instructions

`docs/architecture.md` is the only architecture authority for V1. Do not
recreate, import, or preserve contracts from deleted legacy components, tests,
skills, generated snapshots, or historical plans.

## Product routing

- The user interface is the Codex conversation, not a CLI-first workflow.
- When the user wants to extract decisions from an existing Codex task, follow
  the Capture → Review → Publish flow in the architecture document.
- When the user wants a genuinely new goal or a new developer handoff, run
  Preflight, assemble relevant decision context, and start a new Codex task.
- When the development goal is unchanged, use Codex-native resume/steer
  behavior. ZDecision must not introduce a coordinator or task scheduler for
  that case.
- Use the exact app-server operation mapping in `docs/architecture.md`; do not
  emulate task lifecycle with a new local conversation runtime.
- In V1, invoke those operations through Codex App's native task tools. Do not
  launch another app-server process or add a persistent task daemon.

## Boundaries

- V1 uses this repository, one `main` branch, and `decision-registry/`.
- Only reviewed formal decisions may enter `decision-registry/`.
- Never put raw task content, candidate payloads, private review state,
  workspace snapshots, credentials, or secrets in Git decision storage.
- A model-generated candidate is not a formal decision. Publication always
  requires explicit user review and confirmation.
- Registry unavailable is not the same as an empty Registry.

## Development rules

- Implement one vertical slice at a time; prove its user-visible scenario
  before adding another abstraction. The three slices are delivery checkpoints
  for the complete V1 architecture, not a throwaway validation version.
- Prefer direct data structures and standard-library code in V1.
- Do not add distributed coordination, generalized workflow engines,
  multi-stage approval systems, or speculative migration layers.
- Reuse may occur at the behavior level, but no deleted legacy module or data
  contract may be restored as a dependency.
- Do not document or expose commands that are not implemented and tested.
