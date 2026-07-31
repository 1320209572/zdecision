# Codex Inline UI Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether the installed Codex desktop host renders and can call back from a minimal MCP Apps widget supplied by the ZDecision Plugin.

**Architecture:** Extend the existing Python stdio MCP server with one versioned `ui://` HTML resource, one read-only render tool, and one deterministic probe-action tool. The widget uses only the portable MCP Apps bridge; it does not create a Capture Request or change ZDecision domain state.

**Tech Stack:** Python 3.11+, `mcp>=1.28,<2`, FastMCP, vanilla HTML/CSS/JavaScript, `unittest`.

## Global Constraints

- Do not connect the probe button to Candidate generation.
- Do not add React, Node, a network listener, authentication, or new persistent state.
- The widget must use `_meta.ui.resourceUri`, `text/html;profile=mcp-app`, `ui/initialize`, and `tools/call`.
- A host that exposes only the tools but does not render the resource is a valid negative result.

---

### Task 1: Minimal MCP Apps probe

**Files:**
- Create: `src/zdecision/agent/static/update-probe-v1.html`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `pyproject.toml`
- Test: `tests/test_mcp_ui_probe.py`
- Modify: `tests/test_plugin_contract.py`

**Interfaces:**
- Produces: `create_mcp_server(tools: LocalMcpTools) -> FastMCP`
- Produces: MCP resource `ui://zdecision/update-probe-v1.html`
- Produces: MCP tools `show_zdecision_update` and `acknowledge_zdecision_update`

- [ ] **Step 1: Write failing contract tests**

  Assert that the server lists the versioned UI resource with MIME type
  `text/html;profile=mcp-app`, that `show_zdecision_update` points to it through
  `_meta.ui.resourceUri`, and that the action tool returns a deterministic
  `probe_acknowledged` result without changing the Agent database.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run:

  ```bash
  python -m unittest tests.test_mcp_ui_probe tests.test_plugin_contract -v
  ```

  Expected: failure because the UI resource and probe tools do not exist.

- [ ] **Step 3: Implement the minimal probe**

  Register the resource and two tools in a testable `create_mcp_server`
  function. The widget initializes the MCP Apps bridge, calls
  `acknowledge_zdecision_update` when clicked, and replaces its status text with
  the returned acknowledgement.

- [ ] **Step 4: Verify GREEN and the full suite**

  Run:

  ```bash
  python -m unittest tests.test_mcp_ui_probe tests.test_plugin_contract -v
  python -m unittest discover -s tests -v
  python -m build
  ```

  Expected: all tests pass and both sdist and wheel contain the HTML resource.

- [ ] **Step 5: Verify the installed host boundary**

  Refresh the editable/local package and start a new Codex task with the
  ZDecision Plugin enabled. Invoke `show_zdecision_update`.

  Pass: an inline card appears, clicking the button returns
  `probe_acknowledged`, and no Capture Request is created.

  Negative result: the tool is callable but Codex renders no widget. Record
  that the current Codex host is headless for MCP Apps UI and keep the existing
  browser page as the supported interaction surface.

