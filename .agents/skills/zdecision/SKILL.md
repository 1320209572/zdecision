---
name: zdecision
description: Capture confirmed decisions from an existing Codex task, review and publish them to the ZDecision Registry, or start a new Codex task with applicable formal decisions.
---

# ZDecision

ZDecision is a natural-language Codex workflow for preserving reviewed
decisions without copying whole task histories into project memory. The user
talks to Codex; Codex uses native task tools and the repository's tested
internal command boundary.

## Route the request

- For “compress this task,” “extract its decisions,” or equivalent Capture
  intent, read [references/capture.md](references/capture.md) completely and
  follow it.
- For the same ongoing development goal, continue or steer the existing Codex
  task. Do not Capture merely because the task is long.
- Review/Publish and Preflight/New Task are product intents, but their commands
  are not part of the current Capture slice. Never invent or expose an
  unimplemented command.

## Preserve the boundaries

- Use Codex App's native task tools. Never start a parallel conversation
  runtime, background daemon, or task scheduler.
- Treat every extracted item as a private Candidate. It is not a formal
  Decision until a later review and exact publication confirmation.
- Keep source task content in the source/Capture tasks. Pass only typed IDs and
  structured extraction results to the internal boundary.
- Keep Candidates and operation state in the user-local private store. Only
  reviewed formal Decisions may eventually enter `decision-registry/`.
- Report unavailable, ambiguous, and zero-result states explicitly. Do not
  manufacture a replacement result.
