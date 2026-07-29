from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills" / "zdecision"


class ZDecisionSkillContractTests(unittest.TestCase):
    def capture_text(self) -> str:
        return (SKILL_ROOT / "references" / "capture.md").read_text("utf-8")

    def test_root_skill_routes_templates_without_exposing_cli_as_user_ux(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text("utf-8")

        self.assertRegex(text, re.compile(r"^---\nname: zdecision\n", re.MULTILINE))
        self.assertIn("references/capture.md", text)
        self.assertIn("natural-language", text)
        self.assertIn("template ID", text)
        self.assertIn("business", text)
        self.assertIn("internal", text.lower())

    def test_capture_reference_orders_the_exact_two_stage_protocol(self) -> None:
        text = self.capture_text()
        ordered_phrases = (
            "capture prepare",
            "fork_thread",
            "capture attach",
            "inventory_prompt",
            "--stage inventory",
            "complete-inventory",
            "extraction_prompt",
            "--stage extraction",
            "complete-extraction",
            "capture show",
        )
        positions = [text.index(phrase) for phrase in ordered_phrases]

        self.assertEqual(sorted(positions), positions)
        self.assertNotIn("capture complete --operation-id", text)

    def test_default_and_explicit_template_selection_are_unambiguous(self) -> None:
        text = self.capture_text()

        self.assertIn("defaulting to `business`", text)
        self.assertIn("--template-id TEMPLATE_ID", text)
        self.assertIn("stable template ID", text)
        self.assertIn("verbatim", text)

    def test_continuations_resume_the_frozen_snapshot_for_every_v2_status(self) -> None:
        text = self.capture_text()

        statuses = (
            "prepared",
            "fork_attached",
            "inventory_running",
            "inventory_completed",
            "extraction_running",
            "completed",
            "failed",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.assertRegex(text, re.compile(rf"\| `{status}` \|"))
        self.assertIn("legacy completed", text)
        self.assertIn("capture resume --operation-id ID", text)
        self.assertIn("frozen", text)
        self.assertIn("live template", text)

    def test_both_prompts_are_sent_exactly_and_verbatim_in_one_fork(self) -> None:
        text = self.capture_text()

        self.assertIn("exact frozen `inventory_prompt`", text)
        self.assertIn("exact frozen `extraction_prompt`", text)
        self.assertIn("immediately next Turn", text)
        self.assertIn("same attached fork", text)
        self.assertIn("Do not add", text)
        self.assertIn("Both model Turns must not call tools", text)
        self.assertIn("must not paginate", text)

    def test_each_stage_turn_is_attached_and_reconciled_before_completion(self) -> None:
        text = self.capture_text()

        self.assertIn("attach-turn --operation-id OPERATION_ID --stage inventory", text)
        self.assertIn("attach-turn --operation-id OPERATION_ID --stage extraction", text)
        self.assertIn("read the resulting Turn ID", text)
        self.assertIn("matching Turn", text)
        self.assertIn("send_message_to_thread", text)
        self.assertIn("wait_threads", text)

    def test_wait_timeouts_reconcile_without_recording_model_timeout(self) -> None:
        text = self.capture_text()

        self.assertRegex(
            text,
            re.compile(r"wait_threads.{0,900}timeout", re.IGNORECASE | re.DOTALL),
        )
        self.assertIn("must not call `fail-stage` with `model_timeout`", text)
        self.assertIn("leave the operation running for reconciliation", text)

    def test_terminal_failure_code_set_is_closed_and_timeout_is_unambiguous(
        self,
    ) -> None:
        text = self.capture_text()

        for code in (
            "model_refusal",
            "model_timeout",
            "native_unavailable",
            "model_contract_violation",
        ):
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", text)
        self.assertIn("only allowed `fail-stage` codes", text)
        self.assertIn("definite terminal native Turn timeout", text)
        self.assertIn("never a controller wait timeout", text)
        self.assertIn("Arbitrary failure codes and messages are forbidden", text)

    def test_definite_failure_requires_terminal_evidence_for_the_stored_turn(
        self,
    ) -> None:
        text = self.capture_text()

        self.assertIn("A failure is definite only when", text)
        self.assertIn("stored stage Turn", text)
        self.assertIn("native terminal reason explicitly reports", text)
        self.assertIn("explicit model refusal", text)
        for insufficient_evidence in (
            "controller wait timeout",
            "missing snapshot",
            "commentary",
            "uncertain result",
        ):
            with self.subTest(insufficient_evidence=insufficient_evidence):
                self.assertIn(insufficient_evidence, text)

    def test_native_start_result_table_distinguishes_all_three_semantic_branches(
        self,
    ) -> None:
        text = self.capture_text()
        heading = "### Native start-result decisions"
        self.assertIn(heading, text)
        section = text.split(heading, 1)[1].split("\n### ", 1)[0]
        rows: dict[str, tuple[str, str]] = {}
        for line in section.splitlines():
            if not line.startswith("|"):
                continue
            cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
            if len(cells) != 3 or cells[0] in (
                "Native observation",
                "---",
            ):
                continue
            rows[cells[0].strip("`")] = (cells[1], cells[2])

        self.assertEqual(
            {
                "definite pre-Turn native unavailable",
                "uncertain turn/start result",
                "post-attachment terminal result",
            },
            set(rows),
        )
        preturn_evidence, preturn_action = rows[
            "definite pre-Turn native unavailable"
        ]
        self.assertIn("no Turn was created", preturn_evidence)
        self.assertNotIn("stored stage Turn", preturn_evidence + preturn_action)
        self.assertIn("fail-stage", preturn_action)
        self.assertIn("native_unavailable", preturn_action)
        self.assertIn("fixed sanitized pre-Turn failure", preturn_action)

        uncertain_evidence, uncertain_action = rows[
            "uncertain turn/start result"
        ]
        self.assertIn("no definite Turn ID or terminal outcome", uncertain_evidence)
        self.assertIn("must not call `fail-stage`", uncertain_action)
        self.assertIn("leave the eligible state unchanged", uncertain_action)
        self.assertIn("reconcile", uncertain_action)

        attached_evidence, attached_action = rows[
            "post-attachment terminal result"
        ]
        self.assertIn("stored stage Turn ID exists", attached_evidence)
        self.assertIn("read_thread", attached_action)
        self.assertIn("wait_threads", attached_action)
        self.assertIn("stored-Turn evidence rules", attached_action)

    def test_turn_reconciliation_uses_exact_attached_or_unique_pre_attach_match(
        self,
    ) -> None:
        text = self.capture_text()

        self.assertIn("exact stored Turn ID", text)
        self.assertIn("exact frozen stage prompt", text)
        self.assertIn("attached fork", text)
        self.assertIn("Before attachment", text)
        self.assertIn("single unique Turn", text)
        self.assertIn("correct immediate boundary and order", text)
        self.assertIn("zero or multiple plausible matches", text.lower())
        self.assertIn("must stop without sending another Turn", text)

    def test_invalid_or_corrupt_inventory_never_starts_stage_two(self) -> None:
        text = self.capture_text()

        self.assertIn("invalid JSON", text)
        self.assertIn("no repair prompt", text)
        self.assertIn("missing, corrupt, or digest-mismatched inventory", text)
        self.assertIn("stop at `capture resume`", text)
        self.assertIn("do not send `extraction_prompt`", text)

    def test_terminal_and_zero_candidate_rules_are_explicit(self) -> None:
        text = self.capture_text()

        self.assertIn("failed operations never re-fork", text)
        self.assertIn("zero Candidates", text)
        self.assertIn("successful result", text)
        self.assertIn("model_contract_violation", text)
        self.assertIn("definite terminal failure", text)

    def test_show_surfaces_safe_review_context_only(self) -> None:
        text = self.capture_text()

        self.assertIn("template title", text)
        self.assertIn("template ID", text)
        self.assertIn("revision", text)
        self.assertIn("content digest", text)
        self.assertIn("known_gaps", text)
        self.assertIn("Candidates", text)
        self.assertIn("Do not copy raw source", text)
        self.assertIn("stdin", text)
        self.assertIn("Git", text)

    def test_private_review_shows_validated_candidates_to_the_requesting_user(
        self,
    ) -> None:
        text = self.capture_text()

        self.assertIn("validated Candidate fields", text)
        self.assertIn("requesting user", text)
        self.assertIn("required private Review presentation", text)
        self.assertIn("is allowed", text)
        for forbidden_destination in (
            "Git",
            "Registry",
            "raw model payload",
            "full inventory",
            "frozen prompts",
            "raw source excerpts",
        ):
            with self.subTest(forbidden_destination=forbidden_destination):
                self.assertIn(forbidden_destination, text)

    def test_native_boundaries_bootstrap_and_page_limit_remain_locked(self) -> None:
        text = self.capture_text()

        required = (
            "thread/read",
            "thread/fork",
            "turn/start",
            "completed Turn",
            "read_thread",
            "fork_thread",
            '"turnLimit": 10',
            "python3 -m venv .venv",
            ".venv/bin/python -m zdecision",
            r".venv\Scripts\python.exe -m zdecision",
            "active unfinished Turn",
            "must equal the selected checkpoint",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertNotIn('"turnLimit": 20', text)

    def test_readme_documents_default_and_hypothetical_installed_template(self) -> None:
        text = (REPOSITORY_ROOT / "README.md").read_text("utf-8")

        self.assertIn("压缩任务 `<task-id>` 的决策", text)
        self.assertIn("业务决策压缩模板", text)
        self.assertIn("architecture", text)
        self.assertRegex(text, re.compile(r"architecture.{0,100}(install|安装|cop)", re.I))
        self.assertIn("stable ID", text)
        self.assertIn("title", text)
        self.assertIn("revision", text)
        self.assertIn("two policy files", text)
        self.assertNotIn("capture prepare", text)
        self.assertIn("extractor-v1 completed records remain display-only", text)
        self.assertNotIn("no legacy architecture or compatibility layer", text)

    def test_capture_reference_does_not_restore_forbidden_runtime_or_storage(self) -> None:
        lowered = self.capture_text().lower()

        self.assertNotIn("codex app-server", lowered)
        self.assertNotIn("coordinator", lowered)
        self.assertNotRegex(
            lowered,
            re.compile(r"(store|persist|save).{0,24}(transcript|raw messages?)"),
        )


if __name__ == "__main__":
    unittest.main()
