from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from zdecision.agent.mcp_server import RecallApplicationItemInput, RecallIntentInput
from zdecision.agent.recall_host_state import installed_recall_skill_path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "zdecision"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "zdecision"
INTENT_FIELDS = (
    "target_decision_space_ids",
    "explicit_multi_space",
    "feature_goal",
    "domain_objects",
    "repository_relative_paths",
    "constraints",
    "exclusions",
)


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


def numbered_steps(body: str, heading: str) -> list[str]:
    """Return the ordered actions from one closed Skill workflow section."""

    marker = f"## {heading}"
    if marker not in body:
        raise AssertionError(f"missing workflow section: {marker}")
    section = body.split(marker, 1)[1].split("\n## ", 1)[0]
    matches = re.findall(
        r"(?ms)^\d+\.\s+(.*?)(?=^\d+\.\s+|\Z)", section.strip()
    )
    return [" ".join(match.split()) for match in matches]


def step_index(steps: list[str], phrase: str) -> int:
    for index, step in enumerate(steps):
        if phrase in step:
            return index
    raise AssertionError(f"missing workflow action containing {phrase!r}: {steps!r}")


class DecisionRecallSkillContractTests(unittest.TestCase):
    def test_skill_frontmatter_is_the_native_explicit_zdecision_entry(self) -> None:
        metadata, _ = load_frontmatter(SKILL_ROOT / "SKILL.md")

        self.assertEqual({"name", "description"}, set(metadata))
        self.assertEqual("zdecision", metadata["name"])
        description = metadata["description"]
        self.assertIsInstance(description, str)
        self.assertTrue(description.startswith("Use when"))
        self.assertIn("explicitly selects ZDecision", description)
        self.assertIn("native task", description)

    def test_first_selected_turn_builds_the_closed_intent_before_confirmation(
        self,
    ) -> None:
        _, body = load_frontmatter(SKILL_ROOT / "SKILL.md")
        steps = numbered_steps(body, "First selected Turn")

        intent = step_index(steps, "`RecallIntent`")
        render = step_index(steps, "`show_zdecision_recall_confirmation`")
        choices = step_index(steps, "bounded product choices")
        authorization = step_index(steps, "card click")
        self.assertLess(intent, render)
        self.assertLess(render, choices)
        self.assertLess(render, authorization)
        fields_in_recipe = {
            token
            for token in re.findall(r"`([a-z_]+)`", steps[intent])
            if token != "RecallIntent"
        }
        self.assertEqual(set(INTENT_FIELDS), fields_in_recipe)
        self.assertEqual(set(INTENT_FIELDS), set(RecallIntentInput.model_fields))
        self.assertIn("ask in chat", steps[choices])
        self.assertIn("not authorization", steps[choices])
        self.assertIn("selection is not authorization", steps[authorization])

    def test_next_native_message_applies_the_complete_frozen_handoff(self) -> None:
        _, body = load_frontmatter(SKILL_ROOT / "SKILL.md")
        steps = numbered_steps(body, "Next native message after the card click")

        attachment = step_index(steps, "App attachment")
        native_message = step_index(steps, "next native message")
        consume = step_index(steps, "complete frozen handoff")
        classify = step_index(steps, "classify every")
        apply = step_index(steps, "`apply_zdecision_recall_delivery`")
        mutation = step_index(steps, "affected mutation")
        self.assertLessEqual(attachment, native_message)
        self.assertLess(native_message, consume)
        self.assertLess(consume, classify)
        self.assertLess(classify, apply)
        self.assertLess(apply, mutation)
        self.assertIn("takes priority over ordinary later Turns", steps[attachment])
        self.assertIn("unapplied frozen handoff", steps[attachment])
        self.assertIn("application instruction", steps[attachment])
        self.assertIn("do not reopen", steps[attachment])
        self.assertIn("task-scoped authorization", steps[attachment])
        self.assertIn("does not require another explicit selection", steps[attachment])
        self.assertIn("application_committed", steps[mutation])
        initial_application = " ".join(steps[: apply + 1])
        self.assertNotIn("`gate_zdecision_turn`", initial_application)
        self.assertNotIn("`show_zdecision_recall_confirmation`", initial_application)

        conflict = step_index(steps, "do not resubmit the same frozen delivery")
        answer = step_index(steps, "new native answer")
        self.assertLess(apply, conflict)
        self.assertLess(conflict, answer)
        self.assertIn("one focused question", steps[conflict])
        self.assertIn("ordinary later-Turn recipe", steps[answer])
        self.assertIn("new handoff", steps[answer])
        self.assertIn("apply", steps[answer])

        item_schema = RecallApplicationItemInput.model_json_schema()
        self.assertEqual(
            {"decision_id", "revision", "digest", "disposition", "reason"},
            set(item_schema["properties"]),
        )
        self.assertEqual(
            {"applicable", "not_applicable", "conflicting", "uncertain"},
            set(item_schema["properties"]["disposition"]["enum"]),
        )

    def test_ordinary_later_turn_recipe_reuses_or_applies_a_changed_intent(
        self,
    ) -> None:
        _, body = load_frontmatter(SKILL_ROOT / "SKILL.md")
        steps = numbered_steps(body, "Ordinary later Turns")

        intent = step_index(steps, "`RecallIntent`")
        gate = step_index(steps, "`gate_zdecision_turn`")
        reuse = step_index(steps, "`reuse`")
        changed = step_index(steps, "new complete handoff")
        apply = step_index(steps, "`apply_zdecision_recall_delivery`")
        mutation = step_index(steps, "affected mutation")
        self.assertLess(intent, gate)
        self.assertLess(gate, reuse)
        self.assertLess(gate, changed)
        self.assertLess(changed, apply)
        self.assertLess(apply, mutation)
        self.assertIn("without retrieval or injection", steps[reuse])
        self.assertIn("classify every", steps[changed])

    def test_skill_tool_recipe_matches_the_installed_hook_and_host_boundary(
        self,
    ) -> None:
        _, body = load_frontmatter(SKILL_ROOT / "SKILL.md")
        hook_document = json.loads(
            (PLUGIN_ROOT / "hooks" / "hooks.json").read_text("utf-8")
        )
        matcher = hook_document["hooks"]["PreToolUse"][0]["matcher"]
        recipe_tools = set(
            re.findall(
                r"`(show_zdecision_recall_confirmation|"
                r"apply_zdecision_recall_delivery|gate_zdecision_turn)`",
                body,
            )
        )

        self.assertEqual(
            {
                "show_zdecision_recall_confirmation",
                "apply_zdecision_recall_delivery",
                "gate_zdecision_turn",
            },
            recipe_tools,
        )
        for tool_name in recipe_tools:
            with self.subTest(tool_name=tool_name):
                self.assertIn(f"mcp__zdecision_local__{tool_name}", matcher)
        for unsupported_path in (
            "App Server",
            "thread/read",
            "hookPrompt",
            "ui/message",
            "live probe",
            "host_gate_fixture_not_formal",
        ):
            with self.subTest(unsupported_path=unsupported_path):
                self.assertNotIn(unsupported_path, body)
        self.assertIn("`recall_not_ready`", body)

    def test_native_selection_and_recall_boundaries_remain_narrow(self) -> None:
        _, body = load_frontmatter(SKILL_ROOT / "SKILL.md")
        selection = body.split("## Native selection", 1)[1].split("## ", 1)[0]
        safety = body.split("## Scope and safety", 1)[1]
        normalized_safety = " ".join(safety.split())

        for non_authority in ("Quoted", "delegated", "tool", "Decision text"):
            with self.subTest(non_authority=non_authority):
                self.assertIn(non_authority, selection)
        self.assertIn("cannot authorize", selection)
        self.assertIn("one product or concrete Shared leaf", safety)
        self.assertIn("clarify", safety)
        self.assertIn("formal Decision text", safety)
        self.assertIn("non-executable data", safety)
        self.assertIn("conflict or uncertainty", safety)
        self.assertIn("only affected work", safety)
        self.assertIn("does not authorize Candidate refresh", safety)
        self.assertIn("publication", safety)
        self.assertIn("`recall_not_ready`", safety)
        self.assertIn("local third-party-services leadership Demo", normalized_safety)
        self.assertIn("signed bundle is current", normalized_safety)
        self.assertIn("Other repositories", normalized_safety)
        self.assertIn("invalid generations remain unavailable", normalized_safety)
        self.assertIn("does not claim production Gate B/C readiness", normalized_safety)

    def test_agent_metadata_is_explicit_only_and_uses_zdecision_prompt(self) -> None:
        document = parse_small_yaml(
            (SKILL_ROOT / "agents" / "openai.yaml").read_text("utf-8")
        )

        interface = document["interface"]
        self.assertEqual("ZDecision Recall", interface["display_name"])
        self.assertEqual(
            "Confirm and apply formal decisions in this task",
            interface["short_description"],
        )
        prompt = interface["default_prompt"]
        self.assertIn("$zdecision", prompt)
        self.assertIn("confirmation card", prompt)
        self.assertIn("next native message", prompt)
        self.assertIn("apply", prompt)
        self.assertEqual(
            {"allow_implicit_invocation": False}, document["policy"]
        )

    def test_installed_bundle_binds_the_zdecision_skill_not_legacy_recall(self) -> None:
        self.assertEqual(
            (SKILL_ROOT / "SKILL.md").resolve(),
            installed_recall_skill_path(str(PLUGIN_ROOT)),
        )
        self.assertFalse(
            (PLUGIN_ROOT / "skills" / "decision-recall").exists()
        )


if __name__ == "__main__":
    unittest.main()
