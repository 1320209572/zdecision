from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "zdecision"
MARKETPLACE_PATH = REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json"
EXPECTED_LIFECYCLE_HOOKS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreCompact",
    "PostCompact",
    "PostToolUse",
    "Stop",
    "SessionEnd",
}
PLUGIN_VERSION_PATTERN = re.compile(
    r"0\.1\.0(?:\+codex\.[a-z0-9]+(?:-[a-z0-9]+)*)?"
)


def load_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise AssertionError(f"missing required plugin file: {path}")
    with path.open("rb") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def is_valid_plugin_version(version: object) -> bool:
    return isinstance(version, str) and bool(
        PLUGIN_VERSION_PATTERN.fullmatch(version)
    )


class PluginContractTests(unittest.TestCase):
    def test_repository_marketplace_exposes_production_plugin(
        self,
    ) -> None:
        marketplace = load_json(MARKETPLACE_PATH)

        self.assertEqual("zdecision-local", marketplace["name"])
        self.assertEqual(
            {"displayName": "ZDecision Local"}, marketplace["interface"]
        )
        self.assertEqual(1, len(marketplace["plugins"]))
        plugin = marketplace["plugins"][0]
        self.assertEqual(
            {
                "name": "zdecision",
                "source": {
                    "source": "local",
                    "path": "./plugins/zdecision",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            },
            plugin,
        )
    def test_manifest_points_only_to_bundled_components(self) -> None:
        manifest = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

        self.assertEqual("zdecision", manifest["name"])
        self.assertTrue(is_valid_plugin_version(manifest["version"]))
        self.assertEqual("./skills/", manifest["skills"])
        self.assertEqual("./.mcp.json", manifest["mcpServers"])
        self.assertNotIn("hooks", manifest)
        self.assertTrue((PLUGIN_ROOT / "hooks" / "hooks.json").is_file())
        self.assertFalse((PLUGIN_ROOT / "AGENTS.md").exists())

    def test_manifest_version_rejects_invalid_cachebuster_mutations(self) -> None:
        invalid_versions = (
            "0.2.0+codex.valid",
            "0.1.0+other.valid",
            "0.1.0+codex.first+codex.second",
            "0.1.0+codex.foo.bar",
            "0.1.0+codex.UPPER",
            "0.1.0+codex.-leading",
            "0.1.0+codex.trailing-",
            "0.1.0+codex.repeated--hyphen",
            "0.1.0+codex.",
        )

        for version in invalid_versions:
            with self.subTest(version=version):
                self.assertFalse(is_valid_plugin_version(version))

    def test_manifest_version_allows_optional_sanitized_cachebuster(self) -> None:
        for version in (
            "0.1.0",
            "0.1.0+codex.a",
            "0.1.0+codex.release-20260809",
        ):
            with self.subTest(version=version):
                self.assertTrue(is_valid_plugin_version(version))

    def test_bundled_mcp_invokes_the_lazy_local_agent_entrypoint(self) -> None:
        document = load_json(PLUGIN_ROOT / ".mcp.json")

        self.assertEqual({"mcpServers"}, set(document))
        servers = document["mcpServers"]
        self.assertEqual({"zdecision-local"}, set(servers))
        self.assertEqual("zdecision-agent", servers["zdecision-local"]["command"])
        self.assertEqual(["mcp"], servers["zdecision-local"]["args"])
        serialized = json.dumps(document, sort_keys=True)
        self.assertNotIn("device_token", serialized)
        self.assertNotIn("central_url", serialized)
        self.assertNotIn("organization_id", serialized)
        self.assertNotIn("repository_id", serialized)
        manifest_serialized = json.dumps(
            load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json"),
            sort_keys=True,
        )
        self.assertNotIn("device_token", manifest_serialized)
        self.assertNotIn("central_url", manifest_serialized)
        self.assertNotIn("organization_id", manifest_serialized)
        self.assertNotIn("repository_id", manifest_serialized)

    def test_plugin_registers_five_lifecycle_hooks_and_one_render_matcher(self) -> None:
        document = load_json(PLUGIN_ROOT / "hooks" / "hooks.json")
        hooks = document["hooks"]

        self.assertEqual(EXPECTED_LIFECYCLE_HOOKS | {"PreToolUse"}, set(hooks))
        for event_name in EXPECTED_LIFECYCLE_HOOKS:
            matcher_groups = hooks[event_name]
            with self.subTest(event_name=event_name):
                self.assertEqual(1, len(matcher_groups))
                handlers = matcher_groups[0]["hooks"]
                self.assertEqual(1, len(handlers))
                handler = handlers[0]
                self.assertEqual("command", handler["type"])
                self.assertEqual("zdecision-agent hook", handler["command"])
                if event_name == "SessionStart":
                    self.assertEqual(0, handler["additionalContextLimit"])
                elif event_name == "UserPromptSubmit":
                    self.assertEqual(4000, handler["additionalContextLimit"])
                else:
                    self.assertNotIn("additionalContextLimit", handler)
        self.assertLessEqual(
            hooks["SessionEnd"][0]["hooks"][0]["timeout"], 3
        )
        pre_tool_groups = hooks["PreToolUse"]
        self.assertEqual(1, len(pre_tool_groups))
        self.assertEqual(
            (
                "mcp__zdecision_local__show_zdecision_update|"
                "mcp__zdecision_local__show_zdecision_recall_confirmation|"
                "mcp__zdecision_local__gate_zdecision_turn|"
                "Bash|apply_patch|Edit|Write|Agent|mcp__.*"
            ),
            pre_tool_groups[0]["matcher"],
        )
        self.assertEqual(1, len(pre_tool_groups[0]["hooks"]))
        handler = pre_tool_groups[0]["hooks"][0]
        self.assertEqual("command", handler["type"])
        self.assertEqual("zdecision-agent hook", handler["command"])
        self.assertLessEqual(handler["timeout"], 3)
        self.assertNotIn("additionalContextLimit", handler)
        for event_name in ("PreCompact", "PostCompact"):
            self.assertEqual("manual|auto", hooks[event_name][0]["matcher"])

    def test_plugin_exposes_separate_recall_and_candidate_skills(self) -> None:
        skill_names = {
            path.parent.name
            for path in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")
        }

        self.assertEqual({"zdecision", "candidate-refresh"}, skill_names)
        self.assertFalse(
            (PLUGIN_ROOT / "skills" / "decision-recall").exists()
        )

    def test_candidate_skill_describes_the_page_authorized_workflow(self) -> None:
        skill_path = PLUGIN_ROOT / "skills" / "candidate-refresh" / "SKILL.md"
        self.assertTrue(skill_path.is_file(), f"missing plugin skill: {skill_path}")
        text = skill_path.read_text("utf-8")

        self.assertTrue(text.startswith("---\nname: candidate-refresh\n"))
        self.assertIn("zdecision_status", text)
        self.assertIn("更新候选决策", text)
        self.assertIn("enabled repositories", text)
        self.assertIn("persistent local Agent", text)
        self.assertIn("structured Candidate revisions", text)
        self.assertIn("Review and publication", text)
        self.assertIn("Do not ask", text)
        self.assertIn("Session ID", text)
        self.assertIn("capture CLI", text)
        self.assertNotIn("AGENTS.md", text)

    def test_candidate_skill_presents_the_inline_control_at_approved_boundaries(
        self,
    ) -> None:
        text = (
            PLUGIN_ROOT / "skills" / "candidate-refresh" / "SKILL.md"
        ).read_text("utf-8")

        for required in (
            "completed and verified code-development boundary",
            "enabled repository",
            "render `show_zdecision_update` once",
            "更新候选决策",
            "native user message in the current task",
            "call `zdecision_status` first",
            "`repository_registered`",
            "`repository_enabled`",
            "`active_session_bound`",
            "Use only `repository_registered` and `repository_enabled` as the early gate",
            "`active_session_bound` is diagnostic only",
            "must not grant or deny presentation",
            "must not call any ZDecision tool",
            "<codex_delegation>",
            "send_message_to_thread",
            "turn/steer",
            "must not replace the task's existing goal",
            "never send a prompt, delegation, follow-up, or steer",
            "Rendering the card is not Capture authorization",
            "Session start",
            "intermediate Turns",
            "incomplete or failed validation",
            "non-code work",
            "Duplicate renders have no domain side effect",
            "central page",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertNotIn(
            "render `show_zdecision_update` immediately",
            text,
        )
        self.assertNotIn(
            "`repository_enabled`, and `active_session_bound` are all exactly true",
            text,
        )
        self.assertNotIn("show_zdecision_recall_confirmation", text)
        self.assertNotIn("decide_zdecision_recall", text)

    def test_candidate_skill_defaults_to_the_explicit_inline_refresh_phrase(self) -> None:
        agent_config = (
            PLUGIN_ROOT
            / "skills"
            / "candidate-refresh"
            / "agents"
            / "openai.yaml"
        ).read_text("utf-8")
        manifest = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

        self.assertIn('default_prompt: "更新候选决策"', agent_config)
        prompts = manifest["interface"]["defaultPrompt"]
        self.assertEqual("更新候选决策", prompts[0])
        self.assertIn("ZDecision Recall", prompts[1])
        self.assertLessEqual(len(prompts), 3)
        self.assertIn(
            "allow_implicit_invocation: true", agent_config
        )

    def test_manifest_describes_opt_in_recall_without_automatic_capture(self) -> None:
        manifest = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
        serialized = json.dumps(manifest, sort_keys=True)

        self.assertIn("Session opt-in", serialized)
        self.assertIn("confirmation", serialized.lower())
        self.assertIn("Candidate refresh remains explicit", serialized)
        self.assertNotIn("automatically recalls", serialized)

    def test_plugin_exposes_no_model_based_automatic_capture_tools(
        self,
    ) -> None:
        text = (
            PLUGIN_ROOT / "skills" / "candidate-refresh" / "SKILL.md"
        ).read_text("utf-8")

        for forbidden in (
            "report_work_state",
            "submit_current_boundary",
            "milestone_complete",
            "静默 60",
            "automatic eligibility",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_update_candidates_is_the_only_capture_authority(
        self,
    ) -> None:
        source = REPOSITORY_ROOT / "src" / "zdecision"

        self.assertFalse(
            (source / "capture" / "eligibility.py").exists()
        )
        self.assertFalse(
            (source / "app_server" / "capture_runner.py").exists()
        )

    def test_project_installs_agent_entrypoint_and_bounded_mcp_sdk(self) -> None:
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
            document = tomllib.load(stream)
        project = document["project"]

        self.assertIn("mcp>=1.28,<2", project["dependencies"])
        self.assertEqual(
            "zdecision.agent.cli:main", project["scripts"]["zdecision-agent"]
        )
        self.assertEqual(
            ["static/*.html"],
            document["tool"]["setuptools"]["package-data"].get(
                "zdecision.agent"
            ),
        )


if __name__ == "__main__":
    unittest.main()
