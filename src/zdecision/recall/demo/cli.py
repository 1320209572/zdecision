"""Standalone command-line entry point for the local Recall Demo prototype."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.session import RecallIntent

from zdecision.recall.demo.bundle import (
    DemoBundleError,
    VerifiedDemoBundle,
    build_signed_bundle,
    load_verified_bundle,
)
from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.index import DemoIndex, DemoIndexError
from zdecision.recall.demo.model_store import (
    InstalledModels,
    ModelStoreError,
    load_installed_models,
    prepare_models,
)
from zdecision.recall.demo.retrieval import DemoRetrievalError, HybridDemoRetriever
from zdecision.recall.demo.runtime import (
    DemoRuntimeError,
    ModelRuntimeBundle,
    load_transformers_runtime,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _build_index(bundle: VerifiedDemoBundle, runtime: ModelRuntimeBundle) -> DemoIndex:
    return DemoIndex.build(bundle, runtime)


def _load_runtime(
    profile: DemoRetrievalProfile, installed: InstalledModels
) -> ModelRuntimeBundle:
    return load_transformers_runtime(profile, installed)


@dataclass(frozen=True)
class CliDependencies:
    """Injectable command boundaries; the Hub callable is preparation-only."""

    build_bundle: Callable[..., Path] = build_signed_bundle
    load_bundle: Callable[..., VerifiedDemoBundle] = load_verified_bundle
    prepare_models: Callable[..., InstalledModels] = prepare_models
    load_models: Callable[..., InstalledModels] = load_installed_models
    load_runtime: Callable[..., ModelRuntimeBundle] = _load_runtime
    build_index: Callable[..., DemoIndex] = _build_index
    make_retriever: Callable[[], HybridDemoRetriever] = HybridDemoRetriever
    snapshot_download: Callable[..., str] | None = None


class CliInputError(RuntimeError):
    """A sanitized command input failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("filesystem path must be absolute")
    return path


def _external_write_path(value: str) -> Path:
    path = _absolute_path(value)
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise argparse.ArgumentTypeError("filesystem path is invalid") from None
    if resolved == _REPOSITORY_ROOT or resolved.is_relative_to(_REPOSITORY_ROOT):
        raise argparse.ArgumentTypeError(
            "write target must be outside the repository"
        )
    try:
        repository_stat = _REPOSITORY_ROOT.stat()
        repository_identity = (repository_stat.st_dev, repository_stat.st_ino)
        ancestor = resolved
        while True:
            try:
                ancestor_stat = ancestor.stat()
            except FileNotFoundError:
                pass
            except OSError:
                raise argparse.ArgumentTypeError(
                    "filesystem path is invalid"
                ) from None
            else:
                ancestor_identity = (ancestor_stat.st_dev, ancestor_stat.st_ino)
                if ancestor_identity == repository_identity:
                    raise argparse.ArgumentTypeError(
                        "write target must be outside the repository"
                    )
            parent = ancestor.parent
            if parent == ancestor:
                break
            ancestor = parent
    except argparse.ArgumentTypeError:
        raise
    except OSError:
        raise argparse.ArgumentTypeError("filesystem path is invalid") from None
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zdecision-recall-demo", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-bundle", allow_abbrev=False)
    build.add_argument("--product-root", required=True, type=_absolute_path)
    build.add_argument("--profile", required=True, type=_absolute_path)
    build.add_argument("--private-key", required=True, type=_absolute_path)
    build.add_argument("--key-id", required=True)
    build.add_argument("--output", required=True, type=_external_write_path)

    verify = commands.add_parser("verify-bundle", allow_abbrev=False)
    verify.add_argument("--bundle", required=True, type=_absolute_path)
    verify.add_argument("--trust-root", required=True, type=_absolute_path)

    prepare = commands.add_parser("prepare-models", allow_abbrev=False)
    prepare.add_argument("--profile", required=True, type=_absolute_path)
    prepare.add_argument("--state-dir", required=True, type=_external_write_path)
    prepare.add_argument("--model-cache", required=True, type=_external_write_path)

    status = commands.add_parser("model-status", allow_abbrev=False)
    status.add_argument("--profile", required=True, type=_absolute_path)
    status.add_argument("--state-dir", required=True, type=_external_write_path)

    query = commands.add_parser("query", allow_abbrev=False)
    query.add_argument("--bundle", required=True, type=_absolute_path)
    query.add_argument("--trust-root", required=True, type=_absolute_path)
    query.add_argument("--state-dir", required=True, type=_external_write_path)
    query.add_argument("--intent", required=True, type=_absolute_path)
    return parser


def _emit(value: object, *, stream) -> None:
    stream.write(canonical_json_bytes(value).decode("utf-8"))


def _verify_status(bundle: VerifiedDemoBundle) -> dict[str, object]:
    return {
        "status": "verified",
        "decision_space_id": bundle.decision_space_id,
        "product_name": bundle.product_name,
        "repository": bundle.repository,
        "decision_count": len(bundle.decisions),
        "profile_id": bundle.profile.profile_id,
        "profile_digest": bundle.profile.digest,
        "manifest_digest": bundle.manifest_digest,
    }


def _load_profile(path: Path) -> DemoRetrievalProfile:
    try:
        return DemoRetrievalProfile.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise CliInputError("profile_invalid") from None


def _load_intent(path: Path) -> RecallIntent:
    try:
        return RecallIntent.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise CliInputError("intent_invalid") from None


def _model_ready_status(profile: DemoRetrievalProfile) -> dict[str, object]:
    return {
        "status": "ready",
        "profile_digest": profile.digest,
        "embedding_model": {
            "model_id": profile.embedding.model_id,
            "revision": profile.embedding.revision,
        },
        "reranker_model": {
            "model_id": profile.reranker.model_id,
            "revision": profile.reranker.revision,
        },
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    deps: CliDependencies | None = None,
) -> int:
    """Run the standalone Recall Demo command line interface."""
    arguments = _parser().parse_args(argv)
    dependencies = deps or CliDependencies()
    try:
        if arguments.command == "build-bundle":
            dependencies.build_bundle(
                product_root=arguments.product_root,
                profile_path=arguments.profile,
                private_key_path=arguments.private_key,
                key_id=arguments.key_id,
                output_root=arguments.output,
            )
            _emit({"status": "built"}, stream=sys.stdout)
            return 0
        if arguments.command == "verify-bundle":
            bundle = dependencies.load_bundle(
                bundle_root=arguments.bundle,
                trust_root_path=arguments.trust_root,
            )
            _emit(_verify_status(bundle), stream=sys.stdout)
            return 0
        if arguments.command == "prepare-models":
            profile = _load_profile(arguments.profile)
            snapshot_download = dependencies.snapshot_download
            sanitize_snapshot_errors = snapshot_download is None
            if snapshot_download is None:
                try:
                    from huggingface_hub import snapshot_download
                except ImportError:
                    raise CliInputError("model_resolver_unavailable") from None

            def resolve(model_id: str, revision: str) -> Path:
                try:
                    return Path(
                        snapshot_download(
                            repo_id=model_id,
                            revision=revision,
                            cache_dir=str(arguments.model_cache),
                            local_dir=None,
                        )
                    )
                except (OSError, RuntimeError, ValueError):
                    if not sanitize_snapshot_errors:
                        raise
                    raise CliInputError("model_snapshot_unavailable") from None

            dependencies.prepare_models(
                profile=profile,
                state_root=arguments.state_dir,
                snapshot_resolver=resolve,
            )
            _emit(_model_ready_status(profile), stream=sys.stdout)
            return 0
        if arguments.command == "model-status":
            profile = _load_profile(arguments.profile)
            try:
                dependencies.load_models(profile, arguments.state_dir)
            except ModelStoreError as error:
                _emit(
                    {"code": error.code, "status": "not-ready"},
                    stream=sys.stdout,
                )
                return 1
            _emit(_model_ready_status(profile), stream=sys.stdout)
            return 0
        if arguments.command == "query":
            bundle = dependencies.load_bundle(
                bundle_root=arguments.bundle,
                trust_root_path=arguments.trust_root,
            )
            profile = bundle.profile
            installed = dependencies.load_models(profile, arguments.state_dir)
            runtime = dependencies.load_runtime(profile, installed)
            index = dependencies.build_index(bundle, runtime)
            intent = _load_intent(arguments.intent)
            result = dependencies.make_retriever().retrieve(
                intent, bundle, index, runtime
            )
            _emit(result.to_dict(), stream=sys.stdout)
            return 0
        raise AssertionError("unreachable command")
    except (
        CliInputError,
        DemoBundleError,
        ModelStoreError,
        DemoRuntimeError,
        DemoIndexError,
        DemoRetrievalError,
    ) as error:
        _emit({"code": error.code, "status": "error"}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
