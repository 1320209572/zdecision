"""Internal JSON command boundary used by the repository Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from zdecision.capture.inventory import InventoryValidationError
from zdecision.capture.models import CapturePlan, CaptureRecord, LegacyCaptureRecord
from zdecision.capture.service import (
    CaptureForkAmbiguous,
    CaptureForkConflict,
    CaptureNotFound,
    CaptureService,
    CaptureStateError,
    CaptureTurnConflict,
    ExtractionValidationError,
)
from zdecision.capture.templates import TemplateCatalog, TemplateValidationError
from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import (
    FilePrivateStore,
    InvalidPrivateObjectId,
    PrivateStateCorrupt,
    private_state_root,
)


_OUTPUT_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_ENVELOPE_ROOT = _PACKAGE_ROOT / "capture" / "prompt_contracts"


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentError(message)


class _InvalidJson(ValueError):
    def __init__(self, message: str, output_sha256: str | None = None) -> None:
        self.output_sha256 = output_sha256
        super().__init__(message)


def _output_sha256_argument(value: str) -> str:
    if _OUTPUT_SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _nonempty_argument(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="zdecision")
    commands = parser.add_subparsers(dest="domain", required=True)
    capture = commands.add_parser("capture")
    capture_commands = capture.add_subparsers(dest="action", required=True)

    prepare = capture_commands.add_parser("prepare")
    prepare.add_argument("--thread-id", type=_nonempty_argument, required=True)
    prepare.add_argument("--turn-id", type=_nonempty_argument, required=True)
    prepare.add_argument("--product", required=True)
    prepare.add_argument("--template-id", default="business")

    resume = capture_commands.add_parser("resume")
    resume.add_argument("--operation-id", type=_nonempty_argument, required=True)

    attach = capture_commands.add_parser("attach")
    attach.add_argument("--operation-id", type=_nonempty_argument, required=True)
    attach.add_argument("--fork-thread-id", type=_nonempty_argument, required=True)

    attach_turn = capture_commands.add_parser("attach-turn")
    attach_turn.add_argument("--operation-id", type=_nonempty_argument, required=True)
    attach_turn.add_argument("--stage", choices=("inventory", "extraction"), required=True)
    attach_turn.add_argument("--turn-id", type=_nonempty_argument, required=True)

    complete_inventory = capture_commands.add_parser("complete-inventory")
    complete_inventory.add_argument(
        "--operation-id", type=_nonempty_argument, required=True
    )
    complete_inventory.add_argument("--input", required=True)

    complete_extraction = capture_commands.add_parser("complete-extraction")
    complete_extraction.add_argument(
        "--operation-id", type=_nonempty_argument, required=True
    )
    complete_extraction.add_argument("--input", required=True)

    fail_stage = capture_commands.add_parser("fail-stage")
    fail_stage.add_argument("--operation-id", type=_nonempty_argument, required=True)
    fail_stage.add_argument("--stage", choices=("inventory", "extraction"), required=True)
    fail_stage.add_argument(
        "--code",
        choices=(
            "model_refusal",
            "model_timeout",
            "native_unavailable",
            "model_contract_violation",
        ),
        required=True,
    )
    fail_stage.add_argument("--output-sha256", type=_output_sha256_argument)

    show = capture_commands.add_parser("show")
    show.add_argument("--operation-id", type=_nonempty_argument, required=True)
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


def _template_data(record: CaptureRecord) -> dict[str, object]:
    return {
        "template_id": record.template.template_id,
        "revision": record.template.revision,
        "title": record.template.title,
        "content_digest": record.template.template_source_sha256[:12],
    }


def _plan_data(plan: CapturePlan) -> dict[str, object]:
    data = plan.record.public_dict()
    data.update(
        {
            "template": _template_data(plan.record),
            "replayed": plan.replayed,
            "inventory_prompt": plan.inventory_prompt,
            "extraction_prompt": plan.extraction_prompt,
        }
    )
    return data


def _record_data(record: CaptureRecord) -> dict[str, object]:
    return record.public_dict()


def _decode_utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise _InvalidJson(
            "Stage output was not valid JSON",
            hashlib.sha256(raw).hexdigest(),
        ) from None


def _read_json_text(input_name: str, stdin: TextIO) -> tuple[str, str]:
    if input_name == "-":
        byte_stream = getattr(stdin, "buffer", None)
        if byte_stream is not None:
            raw = byte_stream.read()
            text = _decode_utf8(raw)
        else:
            text = stdin.read()
            try:
                raw = text.encode("utf-8")
            except UnicodeError:
                raw = text.encode("utf-8", errors="surrogatepass")
                raise _InvalidJson(
                    "Stage output was not valid JSON",
                    hashlib.sha256(raw).hexdigest(),
                ) from None
    else:
        raw = Path(input_name).read_bytes()
        text = _decode_utf8(raw)
    return text, hashlib.sha256(raw).hexdigest()


def _reject_json_constant(_: str) -> None:
    raise _InvalidJson("Stage output was not valid JSON")


def _decode_json(text: str) -> object:
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except ValueError:
        raise _InvalidJson("Stage output was not valid JSON") from None


def _complete_stage(
    service: CaptureService,
    operation_id: str,
    stage: str,
    input_name: str,
    stdin: TextIO,
) -> tuple[str, Mapping[str, object]]:
    try:
        text, output_sha256 = _read_json_text(input_name, stdin)
    except _InvalidJson as exc:
        assert exc.output_sha256 is not None
        service.record_invalid_json(operation_id, stage, exc.output_sha256)
        raise
    try:
        output = _decode_json(text)
    except _InvalidJson:
        service.record_invalid_json(operation_id, stage, output_sha256)
        raise
    if stage == "inventory":
        record = service.complete_inventory(operation_id, output)
        return "capture.inventory_completed", _record_data(record)
    service.complete_extraction(operation_id, output)
    record = service.get(operation_id)
    if not isinstance(record, CaptureRecord):
        raise CaptureStateError("Legacy Capture records are read-only")
    return "capture.completed", _record_data(record)


def _load_legacy_candidates(
    record: LegacyCaptureRecord,
    store: FilePrivateStore,
) -> list[object]:
    candidates: list[object] = []
    for candidate_id in record.candidate_ids:
        candidate = store.get_candidate(candidate_id)
        if candidate is None:
            raise CaptureStateError(
                f"Candidate {candidate_id!r} is missing from private state"
            )
        candidates.append(candidate.to_dict())
    return candidates


def _show_data(
    operation_id: str,
    service: CaptureService,
    store: FilePrivateStore,
) -> dict[str, object]:
    record = service.get(operation_id)
    if isinstance(record, LegacyCaptureRecord):
        return {
            "record": {**record.to_dict(), "record_version": record.record_version},
            "legacy": True,
            "candidates": _load_legacy_candidates(record, store),
        }
    inventory = service.get_inventory(operation_id)
    candidates = service.get_candidates(operation_id)
    return {
        "record": record.public_dict(),
        "template": _template_data(record),
        "known_gaps": list(inventory.coverage.known_gaps) if inventory else [],
        "candidates": [candidate.to_dict() for candidate in candidates],
    }


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
            arguments.template_id,
        )
        return "capture.prepared", _plan_data(plan)
    if arguments.action == "resume":
        return "capture.resumed", _plan_data(service.resume(arguments.operation_id))
    if arguments.action == "attach":
        record = service.attach_fork(
            arguments.operation_id,
            arguments.fork_thread_id,
        )
        return "capture.fork_attached", _record_data(record)
    if arguments.action == "attach-turn":
        record = service.attach_stage_turn(
            arguments.operation_id,
            arguments.stage,
            arguments.turn_id,
        )
        return f"capture.{arguments.stage}_running", _record_data(record)
    if arguments.action == "complete-inventory":
        return _complete_stage(
            service,
            arguments.operation_id,
            "inventory",
            arguments.input,
            stdin,
        )
    if arguments.action == "complete-extraction":
        return _complete_stage(
            service,
            arguments.operation_id,
            "extraction",
            arguments.input,
            stdin,
        )
    if arguments.action == "fail-stage":
        record = service.record_stage_failure(
            arguments.operation_id,
            arguments.stage,
            arguments.code,
            arguments.output_sha256,
        )
        return "capture.failed", _record_data(record)
    if arguments.action == "show":
        return "capture.shown", _show_data(arguments.operation_id, service, store)
    raise _ArgumentError(f"Unsupported capture action: {arguments.action!r}")


def _template_root(environ: Mapping[str, str]) -> Path:
    override = environ.get("ZDECISION_TEMPLATE_ROOT")
    return Path(override) if override else _REPOSITORY_ROOT / "decision-templates"


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
        catalog = TemplateCatalog(_template_root(actual_environ), _ENVELOPE_ROOT)
        service = CaptureService(store, catalog)
        kind, data = _run_capture(arguments, service, store, actual_stdin)
        _emit(actual_stdout, _success(kind, data))
        return 0
    except _InvalidJson as exc:
        return _emit_error(actual_stdout, actual_stderr, 2, "invalid_json", exc)
    except InventoryValidationError as exc:
        return _emit_error(actual_stdout, actual_stderr, 2, exc.code, exc)
    except ExtractionValidationError as exc:
        return _emit_error(actual_stdout, actual_stderr, 2, exc.code, exc)
    except TemplateValidationError as exc:
        return _emit_error(actual_stdout, actual_stderr, 2, "invalid_template", exc)
    except InvalidPrivateObjectId as exc:
        return _emit_error(actual_stdout, actual_stderr, 2, "invalid_input", exc)
    except _ArgumentError as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            2,
            "invalid_arguments",
            exc,
        )
    except PrivateStateCorrupt as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            3,
            "private_state_invalid",
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
    except CaptureTurnConflict as exc:
        return _emit_error(
            actual_stdout,
            actual_stderr,
            5,
            "capture_turn_conflict",
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
