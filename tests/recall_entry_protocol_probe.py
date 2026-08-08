"""Test-only Gate 0A probe for the existing Codex Desktop host route."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from zdecision.agent.db import AgentDatabase
from zdecision.app_server.gateway import AppServerGateway, AppServerUnavailable
from zdecision.app_server.jsonl import AppServerTransport, ProcessJsonlTransport


_DESKTOP_PROXY_COMMAND = ("codex", "app-server", "proxy")
_GATE_0A_THREAD_ID = "019fdf3f-2b42-79f1-b049-c8e464c330ab"
_SANITIZED_RESULT = {
    "gate": "0A",
    "route": "host_unix",
    "thread_match": True,
    "endpoint_category": "desktop_default_control_socket",
}


def launch_desktop_proxy(
    process_factory: Callable[[Sequence[str]], subprocess.Popen[str]],
) -> AppServerTransport:
    """Launch the pre-existing Codex Desktop proxy without a shell."""
    return ProcessJsonlTransport(process_factory(_DESKTOP_PROXY_COMMAND))


def forbid_controlled_process() -> AppServerTransport:
    """Keep Gate 0A from spawning the controlled stdio app-server fallback."""
    raise AppServerUnavailable("Controlled app-server fallback is forbidden for Gate 0A")


class _CloseOnceTransport:
    def __init__(self, transport: AppServerTransport) -> None:
        self._transport = transport
        self._closed = False

    def send(self, message: dict[str, object]) -> None:
        self._transport.send(message)

    def receive(self, timeout_seconds: float) -> dict[str, object]:
        return self._transport.receive(timeout_seconds)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._transport.close()


def probe_known_thread(
    *, thread_id: str, transport: AppServerTransport
) -> dict[str, object]:
    """Read one known Thread through the Desktop host route and sanitize the result."""
    owned_transport = _CloseOnceTransport(transport)
    gateway: AppServerGateway | None = None
    try:
        _validate_thread_id(thread_id)
        with tempfile.TemporaryDirectory() as directory:
            database = AgentDatabase.open(Path(directory) / "agent.sqlite3")
            try:
                gateway = AppServerGateway.connect(
                    database=database,
                    host_transport=owned_transport,
                    process_factory=forbid_controlled_process,
                )
                gateway.read_thread_identity(thread_id)
                if gateway.route != "host":
                    raise AppServerUnavailable("Desktop host route is unavailable")
                return dict(_SANITIZED_RESULT)
            finally:
                database.close()
    finally:
        if gateway is not None:
            gateway.close()
        owned_transport.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recall_entry_protocol_probe")
    subcommands = parser.add_subparsers(dest="command", required=True)
    thread = subcommands.add_parser("thread")
    thread.add_argument("--thread-id", required=True)
    arguments = parser.parse_args(argv)

    if arguments.command != "thread":
        parser.error("unsupported probe command")

    try:
        transport = launch_desktop_proxy(_desktop_proxy_popen)
        result = probe_known_thread(thread_id=arguments.thread_id, transport=transport)
    except Exception:
        print(json.dumps({"gate": "0A", "status": "FAIL"}, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


def _desktop_proxy_popen(command: Sequence[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        bufsize=1,
    )


def _validate_thread_id(thread_id: str) -> None:
    if thread_id != _GATE_0A_THREAD_ID:
        raise ValueError("thread_id must equal the Gate 0A task ID")


if __name__ == "__main__":
    raise SystemExit(main())
