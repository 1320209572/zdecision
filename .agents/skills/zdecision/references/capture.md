# Capture an existing Codex task

Use this workflow when the user asks to compress an existing task into private
Candidate decisions. Native tools provide the conversation plane:
`thread/read` selects a stable boundary, `thread/fork` isolates extraction, and
`turn/start` runs extraction in that fork. The `zdecision capture ...`
commands below are internal machine operations; execute them for the user and
show only useful outcomes.

## Bootstrap the internal command

Codex performs this bootstrap; do not ask the user to install or run the
internal CLI.

- On macOS/Linux, if the repository environment is absent, run
  `python3 -m venv .venv`, then `.venv/bin/python -m pip install -e .`.
  Invoke every logical `zdecision ...` command below as
  `.venv/bin/python -m zdecision ...`.
- On Windows, create the environment with `py -3 -m venv .venv`, install with
  `.venv\Scripts\python.exe -m pip install -e .`, and invoke commands with
  `.venv\Scripts\python.exe -m zdecision ...`.
- Reuse a working repository environment. Do not depend on a globally installed
  `zdecision` left by another checkout.

## Inputs

Obtain the source task ID and one product identifier. If the product identifier
is unclear, ask the user before preparing Capture. Once confirmed, pass that
string verbatim on every retry—do not translate, normalize, change case, or
substitute an alias because it participates in stable operation identity.

## Exact sequence

1. Call `read_thread` with
   `{"threadId": "SOURCE_TASK_ID", "turnLimit": 20}`. Follow its older-page
   `cursor` with another `read_thread` call until the latest completed Turn is
   unambiguous. Use that completed Turn ID as the checkpoint. An active
   unfinished Turn is intentionally outside the fork and must never be used as
   the checkpoint. If the source is missing, there is no completed Turn, or the
   requested boundary cannot be established, stop; do not infer or reconstruct
   it.

2. Run:

   ```text
   .venv/bin/python -m zdecision capture prepare --thread-id SOURCE_TASK_ID --turn-id COMPLETED_TURN_ID --product PRODUCT
   ```

   Read the one-object JSON response.

   - New `prepared` result: retain `operation_id` and `extraction_prompt`, then
     continue to step 3.
   - `replayed: true` with `completed`: run `zdecision capture show
     --operation-id OPERATION_ID`, show the stored Candidate list (including an
     explicit zero Candidates result), and stop without forking.
   - `replayed: true` with `fork_attached`: reuse its `fork_thread_id` and
     `extraction_prompt`; skip steps 3 and 4, then reconcile the extraction Turn
     at step 5 before sending anything.
   - Exit 5 with `capture_fork_ambiguous`: retain
     `error.details.operation_id`; a native fork may already exist. Reconcile it
     through the native task UI/tools. If exactly one fork is proven, attach its
     ID to that operation. If it is proven that no fork was created, create
     exactly one and attach it to that same operation. If neither conclusion is
     provable, stop. Never delete private state, change an input, or create an
     unverified replacement.

3. Call `fork_thread` with
   `{"threadId": "SOURCE_TASK_ID", "environment": {"type": "same-directory"}}`.
   This native same-directory fork copies completed history only; it excludes
   the active unfinished Turn. If the call returns a definite child task ID,
   continue immediately. If its result is unknown, do not fork again—stop for
   reconciliation.

4. Before starting extraction, persist the returned child ID:

   ```text
   .venv/bin/python -m zdecision capture attach --operation-id OPERATION_ID --fork-thread-id FORK_TASK_ID
   ```

   The same fork ID is an idempotent retry. Exit 5 with
   `capture_fork_conflict` means a different fork is already attached; stop
   rather than choosing one silently.

5. Verify the fork boundary and reconcile before starting a Turn:

   - Call `read_thread` with
     `{"threadId": "FORK_TASK_ID", "turnLimit": 20}` and page older history as
     needed. Establish the inherited source boundary: before the first exact
     `extraction_prompt` Turn when replaying, or the latest completed inherited
     Turn on a fresh fork. Its Turn ID must equal the selected checkpoint. This
     closes the race where the source completes another Turn between
     `read_thread` and `fork_thread`. If it differs or cannot be proven, do not
     start extraction; stop and report the boundary mismatch.
   - Match the exact `extraction_prompt`. If its extraction Turn is completed,
     reuse that final response and do not send again. If it is active, continue
     to step 6. If the task state cannot prove whether that prompt was already
     sent, stop for reconciliation.
   - Only when the attached fork has no Turn for the exact prompt, call
     `send_message_to_thread` with
     `{"threadId": "FORK_TASK_ID", "prompt": "EXTRACTION_PROMPT"}`, substituting
     the returned `extraction_prompt` verbatim. This is the native `turn/start`
     boundary. Do not add source text from the controlling task.

6. Use `wait_threads` with
   `{"targets": [{"threadId": "FORK_TASK_ID"}]}` until that extraction Turn
   completes. On later waits, pass the returned cursor as the target's
   `afterCursor`. If the final response is truncated, use `read_thread` with
   `includeOutputs: true` on the fork to retrieve the complete final response.
   Accept only its final JSON object; do not combine commentary, intermediate
   tool output, or other messages with it.

7. Feed that exact JSON object over stdin, without shell interpolation or a
   checkout file:

   ```text
   .venv/bin/python -m zdecision capture complete --operation-id OPERATION_ID --input -
   ```

   A valid empty `candidates` array completes Capture successfully. A validation
   error is not permission to invent, repair, or broaden a Candidate in the
   controlling task.

8. Run:

   ```text
   .venv/bin/python -m zdecision capture show --operation-id OPERATION_ID
   ```

   Show each Candidate's claim, future action, scope, and invalidation
   conditions for later review. Call them Candidates, not Decisions. For zero
   Candidates, state clearly that the completed checkpoint contained no
   confirmed durable decision. Do not publish anything.

## Retry and privacy rules

- Re-read the source task only to confirm the same completed checkpoint; keep
  the product string verbatim.
- A completed operation replays its stored Candidate IDs and never starts
  another fork or Candidate set.
- A `fork_attached` operation continues in that exact fork and reconciles the
  extraction Turn before sending, so retry does not start a duplicate Turn.
- A `prepared` retry is intentionally ambiguous and requires reconciliation.
- Pass checkpoint IDs, the attached fork ID, and the structured final JSON
  only. Raw source content remains in native task history and never enters
  private JSON or Git.
