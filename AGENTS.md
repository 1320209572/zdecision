# ZDecision repository instructions

`docs/architecture.md` is the product architecture authority. The active
Plugin detail is
`docs/superpowers/specs/2026-07-30-on-demand-candidate-refresh-design.md`.
Do not continue plans or specifications marked Superseded.

## Product routing

- The Plugin product interface is the ZDecision page plus automatic Codex
  recall, not a CLI-first workflow.
- The Plugin observes enabled repositories locally. Candidate extraction starts
  only when the user clicks **Update Candidates** for a repository.
- The user never supplies Session IDs for the Plugin path. The local Agent
  selects changed eligible Sessions and follows Capture → Review → Publish.
- When the user wants a genuinely new goal or a new developer handoff, run
  Preflight, assemble relevant decision context, and start a new Codex task.
- When the development goal is unchanged, use Codex-native resume/steer
  behavior. ZDecision must not introduce a coordinator or task scheduler for
  that case.
- Use the exact app-server operation mapping in `docs/architecture.md`; do not
  emulate task lifecycle with a new local conversation runtime.
- Codex app-server remains the conversation authority. The installed local
  Agent may use the approved typed app-server Gateway and must maintain the
  persistent request channel required for a later page click.

## Boundaries

- V1 uses this repository, one `main` branch, and `decision-registry/`.
- Only reviewed formal decisions may enter `decision-registry/`.
- Never put raw task content, candidate payloads, private review state,
  workspace snapshots, credentials, or secrets in Git decision storage.
- A model-generated candidate is not a formal decision. Publication always
  requires explicit user review and confirmation.
- Registry unavailable is not the same as an empty Registry.
- Raw Sessions, Prompts, tool output, source code, and diffs never enter the
  central service.

## Development rules

- Implement one vertical slice at a time; prove its user-visible scenario
  before adding another abstraction. The three slices are delivery checkpoints
  for the complete V1 architecture, not a throwaway validation version.
- Prefer direct data structures and standard-library code in V1.
- Do not add distributed coordination, generalized workflow engines,
  multi-stage approval systems, or speculative migration layers.
- Deleted legacy modules and contracts are not dependencies. Licensed current
  third-party leaf code may be copied or adapted only with attribution, bounded
  dependencies, and ZDecision-specific privacy and durability tests.
- Do not document or expose commands that are not implemented and tested.
