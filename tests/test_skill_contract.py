from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills" / "zdecision"


class ZDecisionSkillContractTests(unittest.TestCase):
    def test_root_skill_routes_capture_without_exposing_cli_as_user_ux(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text("utf-8")

        self.assertRegex(text, re.compile(r"^---\nname: zdecision\n", re.MULTILINE))
        self.assertIn("references/capture.md", text)
        self.assertIn("natural-language", text)
        self.assertIn("internal", text.lower())

    def test_capture_reference_names_native_and_domain_boundaries(self) -> None:
        text = (SKILL_ROOT / "references" / "capture.md").read_text("utf-8")

        required = (
            "thread/read",
            "thread/fork",
            "turn/start",
            "completed Turn",
            "read_thread",
            "fork_thread",
            "send_message_to_thread",
            "wait_threads",
            "capture attach",
            "zero Candidates",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_capture_reference_orders_fork_attachment_before_extraction(self) -> None:
        text = (SKILL_ROOT / "references" / "capture.md").read_text("utf-8")

        ordered_phrases = (
            "capture prepare",
            "fork_thread",
            "capture attach",
            "send_message_to_thread",
            "capture complete",
        )
        positions = [text.index(phrase) for phrase in ordered_phrases]

        self.assertEqual(sorted(positions), positions)
        self.assertIn("active unfinished Turn", text)
        self.assertIn("exit 5", text.lower())
        self.assertIn("verbatim", text)

    def test_capture_reference_bootstraps_a_fresh_clone_for_codex(self) -> None:
        text = (SKILL_ROOT / "references" / "capture.md").read_text("utf-8")

        self.assertIn("python3 -m venv .venv", text)
        self.assertIn(".venv/bin/python -m zdecision", text)
        self.assertIn(r".venv\Scripts\python.exe -m zdecision", text)
        self.assertIn("Codex performs this bootstrap", text)

    def test_capture_reference_verifies_the_forked_source_boundary(self) -> None:
        text = (SKILL_ROOT / "references" / "capture.md").read_text("utf-8")

        attach_position = text.index("capture attach")
        boundary_position = text.index("inherited source boundary")
        send_position = text.index("send_message_to_thread")

        self.assertLess(attach_position, boundary_position)
        self.assertLess(boundary_position, send_position)
        self.assertIn("must equal the selected checkpoint", text)

    def test_capture_reference_does_not_restore_forbidden_runtime_or_storage(self) -> None:
        text = (SKILL_ROOT / "references" / "capture.md").read_text("utf-8")
        lowered = text.lower()

        self.assertNotIn("codex app-server", lowered)
        self.assertNotIn("coordinator", lowered)
        self.assertNotRegex(
            lowered,
            re.compile(r"(store|persist|save).{0,24}(transcript|raw messages?)"),
        )


if __name__ == "__main__":
    unittest.main()
