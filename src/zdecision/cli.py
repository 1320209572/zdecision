"""Internal JSON command boundary used by the repository Skill."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from zdecision.capture.models import CapturePlan, CaptureRecord
from zdecision.capture.service import (
    CaptureForkAmbiguous,
    CaptureForkConflict,
    CaptureNotFound,
    CaptureService,
    CaptureStateError,
    ExtractionValidationError,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import (
    FilePrivateStore,
    InvalidPrivateObjectId,
    private_state_root,
)


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentError(message)


class _InvalidJson(ValueError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="zdecision")
    commands = parser.add_subparsers(dest="domain", required=True)
    capture = commands.add_parser("capture")
    capture_commands = capture.add_subparsers(dest="action", required=True)

    prepare = capture_commands.add_parser("prepare")
    prepare.add_argument("--thread-id", required=True)
    prepare.add_argument("--turn-id", required=True)
    prepare.add_argument("--product", required=True)

    attach = capture_commands.add_parser("attach")
    attach.add_argument("--operation-id", required=True)
    attach.add_argument("--fork-thread-id", required=True)

    complete = capture_commands.add_parser("complete")
    complete.add_argument("--operation-id", required=True)
    complete.add_argument("--input", required=True)

    show = capture_commands.add_parser("show")
    show.add_argument("--operation-id", required=True)
    return parser


def _emit(stream: TextIO, value: object) -> None:
    stream.write(canonical_json_bytes(value).decode("utf-8"))


def _success(kind: str, data: Mapping[str, object]) -> dict[str, object]:
    return {"ok": True, "kind": kind, "data": dict(data)}


def _failure(
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": {} if details is None else dict(details),
        },
    }


def _plan_data(plan: CapturePlan) -> dict[str, object]:
    data = plan.record.to_dict()
    data["extraction_prompt"] = plan.extraction_prompt
    data["replayed"] = plan.replayed
    return data


def _record_data(record: CaptureRecord) -> dict[str, object]:
    return record.to_dict()


def _reject_json_constant(value: str) -> None:
    raise _InvalidJson(f"Non-finite JSON number is not allowed: {value}")


def _read_json(
    input_name: str,
    stdin: TextIO,
) -> Mapping[str, object]:
    try:
        text = stdin.read() if input_name == "-" else Path(input_name).read_text("utf-8")
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, _InvalidJson) as exc:
        raise _InvalidJson(str(exc)) from exc
    if not isinstance(value, Mapping):
        raise ExtractionValidationError("Extraction result must be a JSON object")
    return value


def _run_capture(
    arguments: argparse.Namespace,
    service: CaptureService,
    store: FilePrivateStore,
    stdin: TextIO,
) -> tuple[str, Mapping[str, object]]:
    if arguments.action == "prepare":
        plan = service.prepare(
            arguments.thread_id,
            arguments.turn_id,
            arguments.product,
        )
        return "capture.prepared", _plan_data(plan)
    if arguments.action == "attach":
        record = service.attach_fork(
            arguments.operation_id,
            arguments.fork_thread_id,
        )
        return "capture.fork_attached", _record_data(record)
    if arguments.action == "complete":
        extraction = _read_json(arguments.input, stdin)
        result = service.complete(arguments.operation_id, extraction)
        return "capture.completed", result.to_dict()
    if arguments.action == "show":
        record = service.get(arguments.operation_id)
        candidates: list[object] = []
        for candidate_id in record.candidate_ids:
            candidate = store.get_candidate(candidate_id)
            if candidate is None:
                raise CaptureStateError(
                    f"Candidate {candidate_id!r} is missing from private state"
                )
            candidates.append(candidate.to_dict())
        return "capture.shown", {
            "record": record.to_dict(),
            "candidates": candidates,
        }
    raise _ArgumentError(f"Unsupported capture action: {arguments.action!r}")


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run one internal operation and emit exactly one JSON stdout envelope."""

    actual_stdin = sys.stdin if stdin is None else stdin
    actual_stdout = sys.stdout if stdout is None else stdout
    actual_stderr = sys.stderr if stderr is None else stderr
    actual_environ = os.environ if environ is None else environ

    try:
        arguments = _parser().parse_args(argv)
        store = FilePrivateStore(private_state_root(actual_environ))
        service = CaptureService(store)
        kind, data = _run_capture(arguments, service, store, actual_stdin)
        _emit(actual_stdout, _success(kind, data))
        return 0
    except _InvalidJson as exc:
        return _emit_error(actual_stdout, actual_stderr, 2, "invalid_json", exc)
    except (ExtractionValidationError, InvalidPrivateObjectId) as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            2,
            "invalid_extraction"
            if isinstance(exc, ExtractionValidationError)
            else "invalid_input",
            exc,
        )
    except _ArgumentError as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            2,
            "invalid_arguments",
            exc,
        )
    except OSError as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            3,
            "input_unavailable",
            exc,
        )
    except CaptureNotFound as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            3,
            "capture_not_found",
            exc,
        )
    except CaptureForkAmbiguous as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            5,
            "capture_fork_ambiguous",
            exc,
            {"operation_id": exc.operation_id},
        )
    except CaptureForkConflict as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            5,
            "capture_fork_conflict",
            exc,
        )
    except CaptureStateError as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            4,
            "capture_action_required",
            exc,
        )


def _emit_error(
    stdout: TextIO,
    stderr: TextIO,
    exit_code: int,
    error_code: str,
    error: Exception,
    details: Mapping[str, object] | None = None,
) -> int:
    message = str(error)
    _emit(stdout, _failure(error_code, message, details))
    stderr.write(f"{error_code}: {message}\n")
    return exit_code
