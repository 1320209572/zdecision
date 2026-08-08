# Recall entry Gate 0A — Desktop host route

Date: 2026-08-08

Status: **FAIL — Gate 0 blocked**

## Environment

- UTC time: `2026-08-08T05:36:44Z`
- Codex Desktop: `26.803.41515`
- Codex CLI: `0.147.0`
- Python: `3.14.4`
- ZDecision source base: `681dd0aa8d99672f3a25c38771d9879a4251a400`

No socket path, host reply, Prompt, host stdout, or host stderr is retained
here.

## Automated evidence

The required focused test command first produced the intended RED result:

```text
ModuleNotFoundError: No module named 'tests.recall_entry_protocol_probe'
```

After the test-only harness was added, the focused test command completed:

```text
Ran 9 tests in 5.419s
OK
```

The required compile check and `git diff --check` also completed cleanly. The
first sandboxed compile attempt could not create `tests/__pycache__`; the same
compile/diff command was rerun with filesystem escalation and completed with
exit 0.

## Real Gate 0A probe

Exact task ID: `019fdf3f-2b42-79f1-b049-c8e464c330ab`

Command:

```sh
.venv/bin/python -m tests.recall_entry_protocol_probe thread \
  --thread-id 019fdf3f-2b42-79f1-b049-c8e464c330ab
```

Sanitized result:

```json
{"gate":"0A","status":"FAIL"}
```

Result: **FAIL**. The Desktop host Unix route was not proven. Gate 0B was not
run.
