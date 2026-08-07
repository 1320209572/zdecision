from __future__ import annotations

import json
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "plugins" / "zdecision" / "skills" / "decision-recall"


def parse_small_yaml(text: str) -> dict[str, object]:
    """Parse the closed, scalar-only YAML shape used by Skill metadata."""

    document: dict[str, object] = {}
    active_mapping: dict[str, object] | None = None
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, separator, raw_value = line.strip().partition(":")
        if not separator:
            raise AssertionError(f"invalid YAML line: {line}")
        value = raw_value.strip()
        if indent == 0:
            if value:
                document[key] = json.loads(value)
                active_mapping = None
            else:
                active_mapping = {}
                document[key] = active_mapping
        elif indent == 2 and active_mapping is not None:
            active_mapping[key] = json.loads(value)
        else:
            raise AssertionError(f"unsupported YAML shape: {line}")
    return document


def load_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text("utf-8")
    start, separator, body = text.partition("\n---\n")
    if not start.startswith("---\n") or not separator:
        raise AssertionError(f"{path} has no closed YAML frontmatter")
    return parse_small_yaml(start.removeprefix("---\n")), body


class DecisionRecallSkillContractTests(unittest.TestCase):
    def test_skill_frontmatter_is_native_explicit_only(self) -> None:
        metadata, _ = load_frontmatter(SKILL_ROOT / "SKILL.md")

        self.assertEqual({"name", "description"}, set(metadata))
        self.assertEqual("decision-recall", metadata["name"])
        description = metadata["description"]
        self.assertIsInstance(description, str)
        self.assertTrue(description.startswith("Use when"))
        self.assertIn("explicitly selects ZDecision", description)
        self.assertIn("native task", description)

    def test_activation_is_required_before_affected_work_on_first_or_late_selection(
        self,
    ) -> None:
        _, body = load_frontmatter(SKILL_ROOT / "SKILL.md")
        activation = body.split("## Later Turns", 1)[0]

        for turn in ("first Turn", "later Turn"):
            with self.subTest(turn=turn):
                position = activation.index(turn)
                tool_position = activation.index(
                    "`activate_zdecision_recall`", position
                )
                work_position = activation.index("affected development", position)
                self.assertLess(tool_position, work_position)

    def test_later_turns_follow_the_hook_gate_instruction(self) -> None:
        _, body = load_frontmatter(SKILL_ROOT / "SKILL.md")
        later_turns = body.split("## Later Turns", 1)[1].split("## ", 1)[0]

        self.assertIn("ordinary later Turns", later_turns)
        self.assertIn("Hook-supplied", later_turns)
        self.assertIn("`gate_zdecision_turn`", later_turns)
        self.assertIn("affected development", later_turns)

    def test_native_selection_and_recall_boundaries_remain_narrow(self) -> None:
        _, body = load_frontmatter(SKILL_ROOT / "SKILL.md")
        selection = body.split("## Native selection", 1)[1].split("## ", 1)[0]
        safety = body.split("## Scope and safety", 1)[1]

        for non_authority in ("Quoted", "delegated", "tool", "Decision text"):
            with self.subTest(non_authority=non_authority):
                self.assertIn(non_authority, selection)
        self.assertIn("cannot activate", selection)
        self.assertIn("one product or concrete Shared leaf", safety)
        self.assertIn("clarify", safety)
        self.assertIn("formal Decision text", safety)
        self.assertIn("non-executable data", safety)
        self.assertIn("conflict or uncertainty", safety)
        self.assertIn("only affected work", safety)
        self.assertIn("does not authorize Candidate refresh", safety)
        self.assertIn("publication", safety)
        self.assertIn("host_gate_fixture_not_formal", safety)
        self.assertIn("acceptance evidence only", safety)
        self.assertIn("no formal Decision recall", safety)

    def test_agent_metadata_is_explicit_only_and_uses_the_recall_skill_prompt(
        self,
    ) -> None:
        document = parse_small_yaml(
            (SKILL_ROOT / "agents" / "openai.yaml").read_text("utf-8")
        )

        self.assertEqual(
            {
                "display_name": "ZDecision Recall",
                "short_description": "Apply relevant formal decisions in this task",
                "default_prompt": "Use $decision-recall to apply relevant formal decisions in this task.",
            },
            document["interface"],
        )
        self.assertEqual(
            {"allow_implicit_invocation": False}, document["policy"]
        )


if __name__ == "__main__":
    unittest.main()
