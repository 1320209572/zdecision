from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zdecision.agent import cli as agent_cli
from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.demo.config import (
    DemoRecallConfig,
    load_demo_recall_config,
    recall_demo_config_path,
    write_demo_recall_config,
)


FIXTURE = {
    "schema_version": 1,
    "repository_name": "zstack-ui-next",
    "product_name": "third-party-services",
    "decision_space_id": "prod_3e6e73b8defbfee89ce7bf26e739b1dc",
    "registry_product_root": "/private/registry/product",
    "profile_path": "/private/demo-profile.json",
    "model_state_root": "/private/model-state",
    "trust_root_path": "/private/demo-public-key",
    "bundle_state_root": "/private/bundles",
    "signing_private_key_path": "/private/demo-private-key",
    "signing_key_id": "demo-leadership-v1",
}


class _CapturedOutput:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        return self.buffer.write(value.encode("utf-8"))

    def flush(self) -> None:
        return None


def _capture(argv: list[str]) -> tuple[int, str, str]:
    stdout = _CapturedOutput()
    stderr = _CapturedOutput()
    with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
        try:
            code = agent_cli.main(argv)
        except SystemExit as error:
            code = error.code
    return (
        code,
        stdout.buffer.getvalue().decode("utf-8"),
        stderr.buffer.getvalue().decode("utf-8"),
    )


class RecallDemoConfigTests(unittest.TestCase):
    def test_config_is_closed_absolute_and_owner_only(self) -> None:
        """An added field, relative path, insecure mode, or symlink must fail."""
        config = DemoRecallConfig.from_dict(FIXTURE)
        self.assertEqual("zstack-ui-next", config.provider.repository_name)

        with self.assertRaises(ValueError):
            DemoRecallConfig.from_dict({**FIXTURE, "unexpected": True})
        with self.assertRaises(ValueError):
            DemoRecallConfig.from_dict(
                {**FIXTURE, "profile_path": "relative-profile.json"}
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "recall-demo.json"
            path.write_bytes(canonical_json_bytes(FIXTURE))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_demo_recall_config(path)

            target = root / "target.json"
            target.write_bytes(canonical_json_bytes(FIXTURE))
            target.chmod(0o600)
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                load_demo_recall_config(path)

    def test_reader_view_contains_no_private_key_field(self) -> None:
        """Reader-facing config must structurally exclude publisher secrets."""
        config = DemoRecallConfig.from_dict(FIXTURE)
        self.assertEqual(
            (
                "repository_name",
                "product_name",
                "decision_space_id",
                "profile_path",
                "model_state_root",
                "trust_root_path",
                "bundle_state_root",
            ),
            tuple(field.name for field in dataclasses.fields(config.provider)),
        )

    def test_writer_refuses_existing_or_group_readable_file(self) -> None:
        """Immutable config writes must never replace previously accepted bytes."""
        config = DemoRecallConfig.from_dict(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recall-demo.json"
            write_demo_recall_config(path, config)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                write_demo_recall_config(path, config)
            self.assertEqual(original, path.read_bytes())

    def test_writer_failure_leaves_no_final_file_and_allows_retry(self) -> None:
        """A failed publish must not strand a partial create-only config file."""
        config = DemoRecallConfig.from_dict(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "recall-demo.json"
            with patch(
                "zdecision.recall.demo.config.os.write",
                side_effect=OSError("injected write failure"),
            ), self.assertRaises(ValueError):
                write_demo_recall_config(path, config)
            self.assertFalse(path.exists())
            self.assertEqual([], list(root.iterdir()))

            write_demo_recall_config(path, config)
            self.assertEqual(config, load_demo_recall_config(path))

    def test_cli_emits_only_configured_status_not_paths(self) -> None:
        """Operator output exposes only bounded prefixes, never setup material."""
        profile_digest = "0123456789ab" + "c" * 52
        model_digest = "abcdef012345" + "d" * 52
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {
                "registry_product_root": root / "registry",
                "profile": root / "profile.json",
                "model_state_root": root / "models",
                "trust_root": root / "trust.pub",
                "bundle_state_root": root / "bundles",
                "signing_private_key": root / "private.key",
            }
            env = {"ZDECISION_STATE_DIR": str(root / "state")}
            arguments = ["recall-demo", "configure"]
            for name, path in values.items():
                arguments.extend(("--" + name.replace("_", "-"), str(path)))
            arguments.extend(("--signing-key-id", "demo-leadership-v1"))
            with patch.dict(os.environ, env, clear=False), patch(
                "zdecision.agent.cli._validate_recall_demo_setup",
                return_value=(profile_digest, model_digest),
            ):
                code, configured, error = _capture(arguments)
                self.assertEqual(0, code)
                self.assertEqual("", error)
                code, status, error = _capture(["recall-demo", "status"])
            self.assertEqual(0, code)
            self.assertEqual("", error)
            self.assertIn(profile_digest[:12], configured + status)
            self.assertIn(model_digest[:12], configured + status)
            for value in (*values.values(), "demo-leadership-v1", profile_digest, model_digest):
                self.assertNotIn(str(value), configured + status)

    def test_invalid_demo_config_is_bounded(self) -> None:
        """Each pinned identity mutation must fail without leaking local details."""
        for field in ("repository_name", "product_name", "decision_space_id"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = {"ZDECISION_STATE_DIR": str(root)}
                path = recall_demo_config_path(env)
                path.parent.mkdir(parents=True)
                value = {**FIXTURE, field: "corrupt"}
                path.write_bytes(canonical_json_bytes(value))
                path.chmod(0o600)
                with patch.dict(os.environ, env, clear=False):
                    code, _stdout, stderr = _capture(["recall-demo", "status"])
                self.assertEqual(1, code)
                self.assertEqual('{"error":"recall_demo_config_invalid"}', stderr)

    def test_recall_demo_parser_errors_are_bounded(self) -> None:
        """Invalid Recall Demo arguments must not disclose their private values."""
        private_path = "/private/demo-private-key"
        secret = "demo-leadership-v1"
        code, stdout, stderr = _capture(
            ["recall-demo", "status", private_path, secret]
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual('{"error":"recall_demo_config_invalid"}', stderr)
        self.assertNotIn(private_path, stderr)
        self.assertNotIn(secret, stderr)
