# Recall Turn-Gate Intent Contract Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the bounded semantic Recall intent when the trusted Hook replaces model-authored Turn-gate coordinates, and expose that intent as a strict MCP schema.

**Architecture:** The Hook remains the sole authority for `turn_gate_id`, but copies only the model-authored `intent` alongside it. The MCP adapter validates the seven-field intent object before delegating to the unchanged Recall domain gate. No Prompt, transcript, host coordinate, or retrieval behavior is added.

**Tech Stack:** Python 3, Pydantic v2, FastMCP, unittest, SQLite-backed Recall host state.

## Global Constraints

- Preserve the approved inline Recall confirmation flow.
- Do not add formal Decision retrieval; the readiness provider remains unchanged.
- Do not read or persist raw Prompt, transcript, diff, source, or tool output.
- Discard every model-authored top-level coordinate except semantic `intent`.
- Do not touch the protected untracked acceptance or integration files.

---

## Task 1: Restore the trusted Turn-gate input contract

**Files:**

- Modify: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_mcp_recall_host_gate.py`
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/mcp_server.py`

- [ ] Add a real seven-field Recall intent fixture to the Hook tests.
- [ ] Change the Hook rewrite regression to require exactly the trusted `turn_gate_id` plus the unchanged semantic `intent`; assert fake host coordinates are removed.
- [ ] Add a Hook regression proving missing intent is denied and cannot commit the pending gate.
- [ ] Add an MCP contract regression proving `gate_zdecision_turn` exposes a closed seven-field intent object.
- [ ] Run the two focused modules and observe the expected RED against the current implementation.
- [ ] Update the Hook to require and preserve only `tool_input.intent` when injecting the trusted Gate ID.
- [ ] Add a closed Pydantic adapter model for the seven intent fields and pass its normalized dictionary to the existing domain gate.
- [ ] Rerun the focused tests to GREEN.
- [ ] Run the Recall confirmation, plugin, and Skill contract regressions once.
- [ ] Compile the changed package, run `git diff --check`, inspect the final diff, and commit only the intended files.

