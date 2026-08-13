"""Process-boundary wiring for the bounded local Recall demo."""

from __future__ import annotations

import io
import json
import os
import hashlib
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from zdecision.agent import cli
from zdecision.agent import mcp_server
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.agent.hooks import HookResponse
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.repository import RepositoryResolver
from zdecision.central.decision_spaces import EnabledRepository
from zdecision.ids import product_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.demo.config import DemoRecallConfig, write_demo_recall_config
from zdecision.recall.demo import factory as demo_factory
from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.model_store import prepare_models
from zdecision.recall.demo.publication import DemoBundlePublisher
from zdecision.recall.demo.retrieval import DemoRecallResult
from zdecision.recall.provider import UnavailableRecallProvider
from zdecision.recall.session import RecallIntent


PRODUCT_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
PROFILE_PATH = Path(__file__).parents[1] / "src/zdecision/recall/demo/demo-profile.json"
PRODUCT_ROOT = Path(__file__).parents[1] / "decision-registry/products" / PRODUCT_ID
REQUIRED_MODEL_FILES = (
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


def _copy_clone(source_fd: int, destination_dir_fd: int, name: str) -> None:
    destination_fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=destination_dir_fd
    )
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while content := os.read(source_fd, 1024 * 1024):
            os.write(destination_fd, content)
    finally:
        os.close(destination_fd)


def _make_writable(root: Path) -> None:
    for directory, child_directories, files in os.walk(root):
        Path(directory).chmod(0o700)
        for name in child_directories:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o700)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o600)


class _CapturedOutput:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        return self.buffer.write(value.encode("utf-8"))

    def flush(self) -> None:
        return None


class _StoppedMcpServer:
    def run(self, *, transport: str) -> None:
        if transport != "stdio":
            raise AssertionError("MCP must use stdio")


class _DemoVertical:
    """A real owner-configured generation with tiny sealed model fixtures."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_root = root / "state"
        self.config_path = self.state_root / "agent" / "recall-demo.json"
        self.repository = root / "zstack-ui-next"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "ZDecision Tests")
        (self.repository / "README.md").write_text("fixture\n", "utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "fixture")
        self._git("remote", "add", "origin", "https://github.com/OpenAI/example.git")
        self._configure_demo()

    def close(self) -> None:
        _make_writable(self.root)

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments], cwd=self.repository, check=True, capture_output=True
        )

    def _configure_demo(self) -> None:
        snapshots = self.root / "snapshots"
        snapshots.mkdir()
        profile_value = json.loads(PROFILE_PATH.read_text("utf-8"))
        sources: dict[str, Path] = {}
        for role in ("embedding", "reranker"):
            source = snapshots / role
            source.mkdir()
            for name in REQUIRED_MODEL_FILES:
                (source / name).write_bytes(f"{role}:{name}\n".encode())
            model_id = profile_value[role]["model_id"]
            sources[model_id] = source
            profile_value[role]["files"] = {
                name: {
                    "sha256": hashlib.sha256((source / name).read_bytes()).hexdigest(),
                    "size": (source / name).stat().st_size,
                }
                for name in REQUIRED_MODEL_FILES
            }
        profile = DemoRetrievalProfile.from_dict(profile_value)
        profile_path = self.root / "profile.json"
        profile_path.write_bytes(canonical_json_bytes(profile.to_dict()))
        private = Ed25519PrivateKey.generate()
        private_path = self.root / "signing.key"
        private_path.write_bytes(
            private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        )
        private_path.chmod(0o600)
        trust_path = self.root / "trust.pub"
        trust_path.write_bytes(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        trust_path.chmod(0o600)
        model_root = self.root / "models"
        with patch("zdecision.recall.demo.model_store._clone_file", side_effect=_copy_clone):
            prepare_models(
                profile=profile,
                state_root=model_root,
                snapshot_resolver=lambda model_id, _revision: sources[model_id],
            )
        config = DemoRecallConfig.from_dict(
            {
                "schema_version": 1,
                "repository_name": "zstack-ui-next",
                "product_name": "third-party-services",
                "decision_space_id": PRODUCT_ID,
                "registry_product_root": str(PRODUCT_ROOT),
                "profile_path": str(profile_path),
                "model_state_root": str(model_root),
                "trust_root_path": str(trust_path),
                "bundle_state_root": str(self.root / "bundles"),
                "signing_private_key_path": str(private_path),
                "signing_key_id": "demo-leadership-v1",
            }
        )
        DemoBundlePublisher(config.publisher).refresh("a" * 40)
        self.config_path.parent.mkdir(parents=True)
        write_demo_recall_config(self.config_path, config)

    def plugin_root(self) -> Path:
        root = self.root / "plugin-cache" / "zdecision" / "0.1.0"
        skill = root / "skills" / "zdecision" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("---\nname: zdecision\n---\n", "utf-8")
        (root / ".codex-plugin").mkdir(exist_ok=True)
        (root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "zdecision",
                    "version": "0.1.0",
                    "skills": "./skills/",
                    "mcpServers": "./.mcp.json",
                }
            ),
            "utf-8",
        )
        (root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "zdecision-local": {
                            "command": "zdecision-agent",
                            "args": ["mcp"],
                        }
                    }
                }
            ),
            "utf-8",
        )
        hooks = root / "hooks" / "hooks.json"
        hooks.parent.mkdir(exist_ok=True)
        hooks.write_text(
            (Path(__file__).parents[1] / "plugins/zdecision/hooks/hooks.json").read_text("utf-8"),
            "utf-8",
        )
        return root

    @property
    def intent(self) -> dict[str, object]:
        return {
            "target_decision_space_ids": [PRODUCT_ID],
            "explicit_multi_space": False,
            "feature_goal": "Verify consent-time Demo retrieval",
            "domain_objects": ["SecurityServiceInstance"],
            "repository_relative_paths": [
                "packages/products/third-party-services/apps/security-services"
            ],
            "constraints": ["Use only the selected formal Decision Space"],
            "exclusions": ["model-authored configuration paths"],
        }


class RecallDemoWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.state_root = self.root / "state"
        self.config_path = self.state_root / "agent" / "recall-demo.json"

    def _run_hook(self, value: dict[str, object]) -> dict[str, object]:
        stdout = _CapturedOutput()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(value).encode("utf-8")))
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            self.assertEqual(0, cli.main(["hook"]))
        return json.loads(stdout.buffer.getvalue())

    def _run_mcp(self):
        captured: list[object] = []

        def create(_local_tools: object, recall_tools: object) -> _StoppedMcpServer:
            captured.append(recall_tools)
            return _StoppedMcpServer()

        with patch.object(mcp_server, "create_mcp_server", side_effect=create):
            self.assertEqual(0, cli.main(["mcp"]))
        return captured[0]

    def test_hook_and_mcp_load_provider_from_same_config_path(self) -> None:
        """A process boundary regression cannot select differing provider files."""
        with (
            patch.dict(
                os.environ,
                {"ZDECISION_STATE_DIR": str(self.state_root)},
                clear=True,
            ),
            patch(
                "zdecision.recall.demo.factory.configured_recall_provider",
                return_value=UnavailableRecallProvider(),
            ) as configured,
        ):
            self._run_hook({"hook_event_name": "PreToolUse"})
            self._run_mcp()

        self.assertEqual(
            [((self.config_path,), {}), ((self.config_path,), {})],
            [(call.args, call.kwargs) for call in configured.call_args_list],
        )

    def test_missing_config_keeps_unavailable_provider_in_both_processes(self) -> None:
        """Absent owner state stays bounded instead of opening a fallback provider."""
        with patch.dict(
            os.environ,
            {"ZDECISION_STATE_DIR": str(self.state_root)},
            clear=True,
        ), patch(
            "zdecision.agent.hooks.handle_hook",
            return_value=HookResponse(event_id="", output={}),
        ) as handle_hook:
            self._run_hook({"hook_event_name": "PreToolUse"})
            recall_tools = self._run_mcp()

        hook_provider = handle_hook.call_args.kwargs["recall_provider"]
        mcp_provider = recall_tools.handoff_service.provider
        intent = RecallIntent.from_dict(
            {
                "target_decision_space_ids": ["prod_" + "3e6e73b8defbfee89ce7bf26e739b1dc"],
                "explicit_multi_space": False,
                "feature_goal": "Bounded unavailable provider check",
                "domain_objects": ["RecallProvider"],
                "repository_relative_paths": ["src/zdecision/agent/cli.py"],
                "constraints": ["owner-only configuration"],
                "exclusions": ["fallback configuration"],
            }
        )
        for provider in (hook_provider, mcp_provider):
            self.assertIsInstance(provider, UnavailableRecallProvider)
            self.assertEqual(
                "recall_not_ready",
                provider.preflight(
                    repository_id="repo_" + "1" * 32,
                    repository_display_name="zstack-ui-next",
                    intent=intent,
                    now=datetime.now(UTC),
                ).code,
            )

    def test_invalid_config_never_falls_back_to_private_argument_or_environment(self) -> None:
        """A corrupt fixed config cannot select a model- or environment-provided path."""
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text("not valid json", "utf-8")
        self.config_path.chmod(0o600)
        private_path = self.root / "private-recall-demo.json"
        private_path.write_text("{}", "utf-8")
        private_path.chmod(0o600)
        with (
            patch.dict(
                os.environ,
                {
                    "ZDECISION_STATE_DIR": str(self.state_root),
                    "RECALL_DEMO_CONFIG_PATH": str(private_path),
                    "RECALL_DEMO_CONFIG": str(private_path),
                },
                clear=True,
            ),
            patch(
                "zdecision.recall.demo.factory.configured_recall_provider",
                wraps=demo_factory.configured_recall_provider,
            ) as configured,
        ):
            self._run_hook({"hook_event_name": "PreToolUse", "config_path": str(private_path)})
            self._run_mcp()

        self.assertEqual(
            [self.config_path, self.config_path],
            [call.args[0] for call in configured.call_args_list],
        )
        self.assertNotIn(private_path, [call.args[0] for call in configured.call_args_list])


class RecallDemoVerticalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.vertical = _DemoVertical(self.root)
        self.addCleanup(self.vertical.close)
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = cli.database_path(
            {"ZDECISION_STATE_DIR": str(self.vertical.state_root)}
        )

    def _plugin_root(self) -> Path:
        return self.vertical.plugin_root()

    def _hook_attempt(self):
        database = AgentDatabase.open(self.database_path)
        store = RecallHostStore.open(self.database_path)
        self.addCleanup(store.close)
        self.addCleanup(database.close)
        resolver = RepositoryResolver(timeout_seconds=0.5)
        snapshot = resolver.resolve(self.vertical.repository)
        self.assertIsNotNone(snapshot)
        database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=snapshot.repository_id,
                product_id=product_id("Demo Recall Wiring"),
                product_name="Demo Recall Wiring",
                enabled=True,
            )
        )
        database.put_enabled_repository(EnabledRepository(snapshot.repository_id, True))
        provider = demo_factory.configured_recall_provider(self.vertical.config_path)
        self.assertNotIsInstance(provider, UnavailableRecallProvider)
        common = {
            "session_id": "demo-session",
            "turn_id": "demo-turn",
            "cwd": str(self.vertical.repository),
        }
        with patch.dict(os.environ, {"PLUGIN_ROOT": str(self._plugin_root())}, clear=False):
            from zdecision.agent.hooks import handle_hook

            handle_hook(
                {"hook_event_name": "UserPromptSubmit", "prompt": "not retained", **common},
                database=database,
                clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
                repository_resolver=resolver,
                worker_waker=lambda _path: None,
                recall_store=store,
            )
            response = handle_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__zdecision_local__show_zdecision_recall_confirmation",
                    "tool_input": {"intent": self.vertical.intent},
                    **common,
                },
                database=database,
                clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
                repository_resolver=resolver,
                worker_waker=lambda _path: None,
                recall_store=store,
                recall_provider=provider,
            )
        self.assertEqual("allow", response.output["hookSpecificOutput"]["permissionDecision"])
        attempt_id = response.output["hookSpecificOutput"]["updatedInput"]["activation_attempt_id"]
        attempt = store.get_activation_attempt(attempt_id)
        self.assertIsNotNone(attempt)
        return attempt_id, attempt

    def _run_mcp(self, callback) -> None:
        class Server:
            def run(self, *, transport: str) -> None:
                if transport != "stdio":
                    raise AssertionError("MCP must use stdio")
                callback(recall_tools)

        recall_tools = None

        def create(_local_tools, captured_recall_tools):
            nonlocal recall_tools
            recall_tools = captured_recall_tools
            return Server()

        with patch.object(mcp_server, "create_mcp_server", side_effect=create):
            mcp_server.run_mcp(
                database_path=self.database_path,
                config_locator_path=self.root / "missing-agent-config.json",
                recall_demo_config_path=self.vertical.config_path,
                cwd=str(self.vertical.repository),
            )

    def test_real_demo_hook_preflight_and_mcp_enable_are_consent_bound(self) -> None:
        """A configured Demo generation is frozen by Hook and retrieved once after consent."""
        attempt_id, attempt = self._hook_attempt()
        pointer = json.loads((self.root / "bundles" / "current.json").read_text("utf-8"))
        self.assertEqual(pointer["manifest_digest"], attempt.preflight.catalog_digest)
        self.assertEqual(pointer["generation_digest"], attempt.preflight.generation_digest)
        calls: list[str] = []
        results: list[dict[str, object]] = []

        def retrieve(intent, bundle, _index, _runtime):
            calls.append("retrieve")
            with sqlite3.connect(self.database_path, timeout=0.05) as connection:
                connection.execute("CREATE TABLE demo_provider_probe(value TEXT NOT NULL)")
                connection.execute("INSERT INTO demo_provider_probe(value) VALUES ('outside')")
            return DemoRecallResult(
                intent_digest=intent.digest,
                profile_digest=bundle.profile.digest,
                manifest_digest=bundle.manifest_digest,
                items=(),
            )

        def exercise(tools) -> None:
            card = tools.show_recall_confirmation(
                activation_attempt_id=attempt_id,
                intent=self.vertical.intent,
                ui_digest="a" * 64,
            )
            self.assertEqual("pending_confirmation", card["state"])
            self.assertEqual([], calls)
            results.append(tools.decide_recall_confirmation(
                activation_attempt_id=attempt_id, action="enable", current_ui_digest="a" * 64
            ))
            results.append(tools.decide_recall_confirmation(
                activation_attempt_id=attempt_id, action="enable", current_ui_digest="a" * 64
            ))

        with (
            patch("zdecision.recall.demo.provider.load_transformers_runtime", return_value=object()),
            patch("zdecision.recall.demo.provider.build_demo_index", return_value=object()),
            patch("zdecision.recall.demo.provider.HybridDemoRetriever.retrieve", side_effect=retrieve),
        ):
            self._run_mcp(exercise)

        self.assertEqual("delivery_claimed", results[0]["state"])
        self.assertEqual(["retrieve"], calls)
        self.assertEqual("delivery_in_progress", results[1]["code"])
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("outside",), connection.execute("SELECT value FROM demo_provider_probe").fetchone())

    def test_real_demo_pointer_change_between_preflight_and_enable_fails_closed(self) -> None:
        """A replaced current generation cannot inject context after card consent."""
        attempt_id, _attempt = self._hook_attempt()
        result: dict[str, object] = {}

        def exercise(tools) -> None:
            tools.show_recall_confirmation(
                activation_attempt_id=attempt_id,
                intent=self.vertical.intent,
                ui_digest="b" * 64,
            )
            current = self.root / "bundles" / "current.json"
            pointer = json.loads(current.read_text("utf-8"))
            pointer["generation"] += 1
            current.write_bytes(canonical_json_bytes(pointer))
            current.chmod(0o600)
            result.update(tools.decide_recall_confirmation(
                activation_attempt_id=attempt_id, action="enable", current_ui_digest="b" * 64
            ))

        self._run_mcp(exercise)

        self.assertEqual("blocked", result["state"])
        self.assertEqual("delivery_prepare_failed", result["code"])
        store = RecallHostStore.open(self.database_path)
        try:
            delivery = store.delivery_for_attempt(attempt_id)
            self.assertIsNotNone(delivery)
            self.assertIsNone(delivery.context_text)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
