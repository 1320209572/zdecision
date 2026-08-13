"""Base-install boundaries for the optional Recall Demo feature."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CONFIG = {
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


class OptionalRecallDemoDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.state_root = self.root / "state"
        guard = self.root / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(
            """
import builtins

_original_import = builtins.__import__
_blocked = (
    "cryptography",
    "torch",
    "transformers",
    "zdecision.recall.demo.bundle",
    "zdecision.recall.demo.provider",
    "zdecision.recall.demo.publication",
)


def _guarded_import(name, *arguments, **keyword_arguments):
    if name in _blocked or name.startswith(tuple(f"{item}." for item in _blocked)):
        raise ModuleNotFoundError(f"blocked optional import: {name}")
    return _original_import(name, *arguments, **keyword_arguments)


builtins.__import__ = _guarded_import
""".lstrip(),
            "utf-8",
        )
        self.environment = os.environ.copy()
        self.environment["ZDECISION_STATE_DIR"] = str(self.state_root)
        self.environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (str(guard), self.environment.get("PYTHONPATH"))
            if value
        )

    def _run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _write_valid_config(self) -> Path:
        path = self.state_root / "agent" / "recall-demo.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(CONFIG, sort_keys=True, separators=(",", ":")) + "\n")
        path.chmod(0o600)
        return path

    def test_base_startup_with_missing_demo_config_imports_no_optional_stack(self) -> None:
        """Missing config must start ordinary Central, Hook, and MCP without Demo extras."""
        completed = self._run(
            r'''
import io
import json
import argparse
import os
from pathlib import Path
from unittest.mock import patch

from zdecision.central.web.application import CentralWebApplication
from zdecision.central import cli as central_cli
from zdecision.agent import cli as agent_cli
from zdecision.agent import mcp_server
from zdecision.recall.provider import UnavailableRecallProvider

scratch = Path(os.environ["ZDECISION_STATE_DIR"]).parent
central_arguments = argparse.Namespace(
    host="127.0.0.1",
    port=8765,
    config=str(scratch / "central.json"),
    database=str(scratch / "central.sqlite3"),
    registry_repository_root=str(Path.cwd()),
)
central_config = {
    "organization_id": "org_demo",
    "user_id": "user_demo",
    "device_id": "device_demo",
    "device_token_sha256": "a" * 64,
    "repositories": [],
    "catalog_groups": [],
    "decision_spaces": [],
    "repository_routes": [],
}
with patch.object(central_cli, "_load_central_config", return_value=central_config), patch.object(central_cli, "_registry_repository_root", return_value=Path.cwd()), patch.object(central_cli, "_synchronize_registry_on_startup", return_value=None), patch("uvicorn.run") as run_server:
    assert central_cli._run_server(central_arguments) == 0
run_server.assert_called_once()


class Output:
    def __init__(self):
        self.buffer = io.BytesIO()
    def write(self, value):
        return self.buffer.write(value.encode("utf-8"))
    def flush(self):
        return None


stdin = io.TextIOWrapper(io.BytesIO(b'{"hook_event_name":"PreToolUse"}'))
stdout = Output()
with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
    assert agent_cli.main(["hook"]) == 0
assert json.loads(stdout.buffer.getvalue()) == {}

captured = []
class Server:
    def run(self, *, transport):
        assert transport == "stdio"
def create(_local, recall):
    captured.append(recall.handoff_service.provider)
    return Server()
with patch.object(mcp_server, "create_mcp_server", side_effect=create):
    assert agent_cli.main(["mcp"]) == 0
assert isinstance(captured[0], UnavailableRecallProvider)
assert central_cli.build_parser().prog == "zdecision-central"
assert CentralWebApplication.__name__ == "CentralWebApplication"
print("base-startup-ok")
'''
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("base-startup-ok\n", completed.stdout)

    def test_valid_demo_config_without_optional_dependencies_is_bounded(self) -> None:
        """Requesting uninstalled Demo support must never leak import or path details."""
        config_path = self._write_valid_config()
        completed = self._run(
            f'''
import argparse
import io
import json
from pathlib import Path
from unittest.mock import patch

from zdecision.central import cli as central_cli
from zdecision.agent import cli as agent_cli
from zdecision.agent import mcp_server
from zdecision.recall.provider import UnavailableRecallProvider

root = Path({str(self.root)!r})
arguments = argparse.Namespace(
    host="127.0.0.1",
    port=8765,
    config=str(root / "central.json"),
    database=str(root / "central.sqlite3"),
    registry_repository_root=str(root / "registry"),
)
with patch.object(central_cli, "_registry_repository_root", return_value=root / "registry"), patch.object(central_cli, "_load_central_config", return_value={{}}):
    try:
        central_cli._run_server(arguments)
    except central_cli.CentralCliError as error:
        assert error.code == "recall_demo_config_invalid"
        assert str(error) == "recall_demo_config_invalid"
    else:
        raise AssertionError("configured Demo without its extra must fail bounded")

class Output:
    def __init__(self):
        self.buffer = io.BytesIO()
    def write(self, value):
        return self.buffer.write(value.encode("utf-8"))
    def flush(self):
        return None

stdin = io.TextIOWrapper(io.BytesIO(b'{{"hook_event_name":"PreToolUse"}}'))
stdout = Output()
with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
    assert agent_cli.main(["hook"]) == 0
assert json.loads(stdout.buffer.getvalue()) == {{}}

status_stdout = Output()
status_stderr = Output()
with patch("sys.stdout", status_stdout), patch("sys.stderr", status_stderr):
    assert agent_cli.main(["recall-demo", "status"]) == 0
status = json.loads(status_stdout.buffer.getvalue())
assert status["status"] == "invalid"
assert status_stderr.buffer.getvalue() == b""

captured = []
class Server:
    def run(self, *, transport):
        assert transport == "stdio"
def create(_local, recall):
    captured.append(recall.handoff_service.provider)
    return Server()
with patch.object(mcp_server, "create_mcp_server", side_effect=create):
    assert agent_cli.main(["mcp"]) == 0
assert isinstance(captured[0], UnavailableRecallProvider)
print("configured-without-extra-bounded")
'''
        )

        combined = completed.stdout + completed.stderr
        self.assertEqual(0, completed.returncode, combined)
        self.assertEqual("configured-without-extra-bounded\n", completed.stdout)
        self.assertNotIn("ModuleNotFoundError", combined)
        for private in CONFIG.values():
            if isinstance(private, str) and private.startswith("/private/"):
                self.assertNotIn(private, combined)
        self.assertNotIn(str(config_path), combined)


if __name__ == "__main__":
    unittest.main()
