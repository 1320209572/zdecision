# Capture an existing Codex task

Use this workflow when the user asks to compress an existing task into private
Candidate decisions. Native tools provide the conversation plane:
`thread/read` selects a stable boundary, `thread/fork` isolates the work, and
`turn/start` runs each model stage in that fork. The commands below are an
internal machine boundary. Execute them for the user and show only safe review
context.

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
is unclear, ask before preparing Capture. Pass it verbatim on every retry
because it participates in stable operation identity.

Select a stable template ID from the user's natural-language request,
defaulting to `business` when none is named. Pass an explicit ID verbatim. A
template title is display metadata, not an alias.

## Exact initial sequence

1. Call `read_thread` with
   `{"threadId": "SOURCE_TASK_ID", "turnLimit": 10}`. Follow its older-page
   `cursor` until the latest completed Turn is unambiguous. Use that completed
   Turn ID as the checkpoint. An active unfinished Turn is outside the fork and
   must not be used. If the source, completed boundary, or requested checkpoint
   cannot be established, stop instead of inferring it.

2. Run:

   ```text
   .venv/bin/python -m zdecision capture prepare --thread-id SOURCE_TASK_ID --turn-id COMPLETED_TURN_ID --product PRODUCT --template-id TEMPLATE_ID
   ```

   Read the single JSON response. Retain its `operation_id`, selected
   checkpoint, and frozen template snapshot. For a new `prepared` operation,
   continue. For a replay or ambiguous result, use the state table below.

3. Call `fork_thread` with
   `{"threadId": "SOURCE_TASK_ID", "environment": {"type": "same-directory"}}`.
   This same-directory fork copies completed history only. If it returns a
   definite child task ID, continue. If the result is unknown, do not fork
   again; leave the operation ready for reconciliation.

4. Persist the definite child ID before starting either model Turn:

   ```text
   .venv/bin/python -m zdecision capture attach --operation-id OPERATION_ID --fork-thread-id FORK_TASK_ID
   ```

   The same ID is an idempotent retry. A different attached ID is a conflict;
   stop instead of choosing one silently.

5. Reconcile and run Stage 1 in the attached fork.

   - Call `read_thread` on `FORK_TASK_ID` with `{"turnLimit": 10}`. On a fresh
     fork, its latest inherited completed Turn must equal the selected checkpoint.
     On a retry, establish that same inherited boundary before the
     matching model Turn. If the boundary differs or is uncertain, stop.
   - Match the exact frozen `inventory_prompt`. If the matching Turn exists,
     reuse it. Only if no matching Turn exists, call `send_message_to_thread`
     with that prompt verbatim. Do not add source text, explanations, repair
     wording, or any other instruction.
   - Immediately read the resulting Turn ID from the fork. Persist it before
     waiting:

     ```text
     .venv/bin/python -m zdecision capture attach-turn --operation-id OPERATION_ID --stage inventory --turn-id INVENTORY_TURN_ID
     ```

   - Use `wait_threads` for that exact matching Turn. Reuse its cursor on later
     waits. Both model Turns must not call tools and must not paginate; each must
     produce exactly one final JSON object. If Stage 1 used a tool or emitted
     non-final processing output, call `fail-stage` with
     `model_contract_violation` and stop. If the final response is invalid JSON,
     submit it once and stop after validation; there is no repair prompt.

6. Feed only Stage 1's exact final JSON object over stdin:

   ```text
   .venv/bin/python -m zdecision capture complete-inventory --operation-id OPERATION_ID --input -
   ```

   Validation must succeed before Stage 2. The private inventory is stored with
   its digest; do not copy its contents into the controlling conversation.

7. Reconcile and run Stage 2 as the immediately next Turn in the same attached fork.

   - Use the exact frozen `extraction_prompt` returned by the operation. If a
     matching Turn already exists, reuse it. Only if none exists, call
     `send_message_to_thread` with the prompt verbatim. Do not add inventory,
     source text, repair wording, or pagination instructions.
   - Read the resulting Turn ID and persist it before waiting:

     ```text
     .venv/bin/python -m zdecision capture attach-turn --operation-id OPERATION_ID --stage extraction --turn-id EXTRACTION_TURN_ID
     ```

   - Use `wait_threads` for that exact matching Turn. If it called a tool or
     emitted non-final processing output, record `model_contract_violation` and
     stop. Invalid JSON is submitted once; never start a repair Turn.

8. Feed only Stage 2's exact final JSON object over stdin:

   ```text
   .venv/bin/python -m zdecision capture complete-extraction --operation-id OPERATION_ID --input -
   ```

   A valid empty `candidates` array is a successful result with zero Candidates.

9. Run:

   ```text
   .venv/bin/python -m zdecision capture show --operation-id OPERATION_ID
   ```

   Present the private Candidates and safe review context: template title,
   template ID, revision, content digest, and `known_gaps`. Show each Candidate's
   claim, future action, scope, and invalidation conditions. Do not publish.

## Continuation and reconciliation

Every continuation after initial prepare begins with
`capture resume --operation-id ID`. Resume returns the frozen template snapshot
and prompts; it must not replace them with a live template edit. Continue from
the exact returned status:

| Status | Continuation |
| --- | --- |
| `prepared` | Reconcile native history. Attach one proven existing fork, or create exactly one only when it is proven that no fork exists. |
| `fork_attached` | Verify the inherited boundary, then reconcile or start the Stage 1 Turn in that fork. |
| `inventory_running` | Reconcile the stored inventory Turn ID and matching Turn; wait or submit its final response without sending again. |
| `inventory_completed` | Resume verifies the private artifact and digest; then reconcile or start Stage 2 in the same fork. |
| `extraction_running` | Reconcile the stored extraction Turn ID and matching Turn; wait or submit its final response without sending again. |
| `completed` | Show the stored result. Never fork, send, or create duplicate Candidates. |
| `failed` | Show the sanitized terminal failure and stop; failed operations never re-fork. |

A legacy completed record is display-only: show its stored Candidates and do
not resume, fork, or migrate it implicitly.

For `inventory_completed`, missing, corrupt, or digest-mismatched inventory is a
hard stop at `capture resume`; do not send `extraction_prompt`. Never reconstruct
private inventory from task history.

Turn matching is exact. Once a stage Turn ID is attached, the matching Turn is
the exact stored Turn ID with the exact frozen stage prompt in the attached fork.
A same-prompt Turn with a different ID or in another fork never matches.
Before attachment after an uncertain `turn/start` result, reconcile only a single unique Turn.
It must be in the attached fork with that exact prompt and the correct immediate boundary and order.
Zero or multiple plausible matches is ambiguous and must stop without sending another Turn.

Apply the following decision table immediately after any attempt to start a
stage. Do not assume that every native start call creates a Turn.

### Native start-result decisions

| Native observation | Required evidence | Required action |
| --- | --- | --- |
| `definite pre-Turn native unavailable` | The native start result explicitly reports that the required capability is unavailable and confirms that no Turn was created. | Call `fail-stage --stage STAGE --code native_unavailable`; this records the fixed sanitized pre-Turn failure and stops the operation. |
| `uncertain turn/start result` | The start result provides no definite Turn ID or terminal outcome. | You must not call `fail-stage`; leave the eligible state unchanged and reconcile the exact prompt and immediate boundary before any later send. |
| `post-attachment terminal result` | A stored stage Turn ID exists and identifies the attached Turn. | Use `read_thread` or `wait_threads` and apply the stored-Turn evidence rules below; only then record an evidenced terminal failure. |

The pre-Turn unavailable branch is terminal because the native result is
explicit about both unavailability and the absence of a created Turn. An
unknown, missing, or ambiguous start result is the uncertain branch, even when
the capability might be unavailable; keep reconciling and never infer a
failure from uncertainty.

### Post-attachment failure evidence

Before completing either stage, reconcile the stored Turn ID against the
matching Turn and accept only that Turn's final response. A `wait_threads`
timeout or uncertain result from the native tool is not a definite model failure: keep
reconciling the same Turn, leave the operation running for reconciliation, and
must not call `fail-stage` with `model_timeout`. Never create a replacement
fork or Turn merely because a wait expired.

The only allowed `fail-stage` codes are:

- `model_refusal` when the attached model Turn definitely refuses;
- `model_timeout` only for a definite terminal native Turn timeout, never a controller wait timeout;
- `native_unavailable` for either the definite pre-Turn branch above or when an
  attached stored Turn's terminal reason explicitly reports unavailable; and
- `model_contract_violation` when the Turn uses tools or produces non-final
  processing output instead of its one final JSON object.

For post-attachment failures, use this scoped rule. A failure is definite only when
`read_thread` or `wait_threads` identifies the stored stage Turn as terminal
and the allowed code is directly evidenced. Its native terminal reason explicitly reports timeout or unavailable for those
codes, or its final response is an explicit model refusal. A terminal
`model_contract_violation` requires recorded tool use or non-final processing
output. These stored-Turn evidence requirements apply only after a Turn ID
exists. A controller wait timeout, missing snapshot, commentary, or uncertain
result never qualifies; leave the operation running for reconciliation.

Record a definite terminal failure once and stop. Arbitrary failure codes and messages are forbidden.
Use the internal command's fixed sanitized message. Do not repair, retry with
extra wording, paginate, or fork again.

## Privacy and output rules

- Showing validated Candidate fields to the requesting user in the controlling
  Codex conversation is the required private Review presentation and is allowed.
- Here, private forbids exposure to Git or the Registry and forbids showing a
  raw model payload, full inventory, frozen prompts, or raw source excerpts. It
  does not hide validated Candidate content from its owner.
- Do not copy raw source excerpts to stdin, private state, Candidate fields, or
  Git. Raw source remains in native task history.
- Feed only a model Turn's structured final JSON to its matching completion
  command. Do not combine commentary or intermediate output with it.
- Never expose private paths or model-authored payloads in error text.
- Call extracted items Candidates, not Decisions. Zero Candidates is explicit,
  successful completion, not an error and not permission to invent a result.
