---
name: zdecision
description: Use when a user wants to capture, review, publish, or apply durable decisions from Codex tasks with a local ZDecision repository.
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
- Route a named Capture template by its stable template ID. If the user does
  not name one, use the default `business` template. Treat the template title
  as display metadata, never as an alias.
- For the same ongoing development goal, continue or steer the existing Codex
  task. Do not Capture merely because the task is long.
- For Review or Publish intent after a completed Capture, read
  [references/review-publish.md](references/review-publish.md) completely and
  follow it. Review acceptance and publication authorization are separate.
- Preflight/New Task remains outside the implemented slice. Never invent an
  unimplemented command.

## Preserve the boundaries

- Use Codex App's native task tools. Never start a parallel conversation
  runtime, background daemon, or task scheduler.
- Treat every extracted item as a private Candidate. It is not a formal
  Decision until a later review and exact publication confirmation.
- Keep source task content in the source/Capture tasks. Pass only typed IDs and
  structured stage results to the internal boundary.
- Keep Candidates and operation state in the user-local private store. Only
  reviewed formal Decisions may eventually enter `decision-registry/`.
- Report unavailable, ambiguous, and zero-result states explicitly. Do not
  manufacture a replacement result.
