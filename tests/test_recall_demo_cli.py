"""Boundary tests for the standalone Recall Demo command line interface."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.session import RecallIntent
from zdecision.registry.models import DecisionRevision

from zdecision.recall.demo.bundle import DemoBundleError
from zdecision.recall.demo.cli import CliDependencies, main
from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.index import DemoIndexError
from zdecision.recall.demo.model_store import (
    InstalledModels,
    ModelStoreError,
    prepare_models,
)
from zdecision.recall.demo.retrieval import (
    DemoRecallResult,
    DemoRetrievalError,
    RankedDemoDecision,
)
from zdecision.recall.demo.runtime import DemoRuntimeError


ROOT = Path(__file__).parents[1]
PROFILE_PATH = ROOT / "src/zdecision/recall/demo/demo-profile.json"
PRODUCT_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
PRODUCT_ROOT = ROOT / "decision-registry/products" / PRODUCT_ID
CLEANUP_ID = "dec_aac76c0a67bc535766c741f80066c706"
CLEANUP_REVISION_PATH = (
    PRODUCT_ROOT / "decisions" / CLEANUP_ID / "r0001.json"
)
REQUIRED_MODEL_FILES = (
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


def _capture(argv: list[str], *, deps: CliDependencies | None = None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv, deps=deps)
    return code, stdout.getvalue(), stderr.getvalue()


def _write_keys(root: Path) -> tuple[Path, Path, bytes]:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    private_path = root / "demo-private.key"
    public_path = root / "demo-trust-root.pub"
    private_path.write_bytes(private_bytes)
    public_path.write_bytes(public_bytes)
    private_path.chmod(0o600)
    return private_path, public_path, private_bytes


def _unexpected_dependency(*args, **kwargs):
    raise AssertionError("domain dependency called before path validation")


def _raise(error: Exception) -> None:
    raise error


def _profile() -> DemoRetrievalProfile:
    return DemoRetrievalProfile.from_dict(
        json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    )


def _make_snapshot(root: Path, role: str) -> Path:
    snapshot = root / role
    snapshot.mkdir(parents=True)
    for name in REQUIRED_MODEL_FILES:
        (snapshot / name).write_bytes(f"{role}:{name}\n".encode())
    return snapshot


def _profile_for_snapshots(
    snapshots: dict[str, Path],
) -> DemoRetrievalProfile:
    value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    for role in ("embedding", "reranker"):
        snapshot = snapshots[value[role]["model_id"]]
        value[role]["files"] = {
            name: {
                "sha256": hashlib.sha256((snapshot / name).read_bytes()).hexdigest(),
                "size": (snapshot / name).stat().st_size,
            }
            for name in REQUIRED_MODEL_FILES
        }
    return DemoRetrievalProfile.from_dict(value)


def _copy_clone(source_fd: int, destination_dir_fd: int, name: str) -> None:
    destination_fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=destination_dir_fd,
    )
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while chunk := os.read(source_fd, 1024 * 1024):
            os.write(destination_fd, chunk)
    finally:
        os.close(destination_fd)


def _prepare_tiny(*, profile, state_root, snapshot_resolver) -> InstalledModels:
    with patch(
        "zdecision.recall.demo.model_store._clone_file",
        side_effect=_copy_clone,
    ):
        return prepare_models(
            profile=profile,
            state_root=state_root,
            snapshot_resolver=snapshot_resolver,
        )


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, child_directories, files in os.walk(root):
        directory_path = Path(directory)
        directory_path.chmod(0o700)
        for name in child_directories:
            path = directory_path / name
            if not path.is_symlink():
                path.chmod(0o700)
        for name in files:
            path = directory_path / name
            if not path.is_symlink():
                path.chmod(0o600)


def _intent_value() -> dict[str, object]:
    return {
        "target_decision_space_ids": [PRODUCT_ID],
        "explicit_multi_space": False,
        "feature_goal": "在安全服务实例列表中展示后端已授权的实例清理入口",
        "domain_objects": [
            "SecurityServiceInstance",
            "cleanupOwnResources",
            "cleanupAnyResources",
        ],
        "repository_relative_paths": [
            "packages/products/third-party-services/apps/security-services/src/api/security-instance-actions.ts"
        ],
        "constraints": ["必须遵循后端动作授权结果"],
        "exclusions": ["不修改清理失败重试流程"],
    }


def _write_intent(root: Path, value: object | None = None) -> Path:
    path = root / "intent.json"
    path.write_bytes(canonical_json_bytes(_intent_value() if value is None else value))
    return path


def _cleanup_revision() -> DecisionRevision:
    return DecisionRevision.from_dict(
        json.loads(CLEANUP_REVISION_PATH.read_text(encoding="utf-8"))
    )


def _tree_fingerprint(root: Path) -> tuple[tuple[object, ...], ...]:
    records = []
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            records.append((relative, stat.st_mode, stat.st_mtime_ns, path.readlink()))
        elif path.is_file():
            records.append(
                (
                    relative,
                    stat.st_mode,
                    stat.st_size,
                    stat.st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        else:
            records.append((relative, stat.st_mode, stat.st_mtime_ns))
    return tuple(records)


class RecallDemoCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.external_root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        _make_tree_writable(self.external_root)
        self.temporary.cleanup()

    def test_cli_module_exposes_main(self) -> None:
        """Removing the standalone entry point must break the operator surface."""
        self.assertTrue(callable(main))

    def test_every_relative_filesystem_argument_fails_before_dependencies(self) -> None:
        """Letting one relative path through could target repository-owned state."""
        absolute = str(self.external_root / "absolute")
        cases = (
            (
                [
                    "build-bundle",
                    "--product-root", absolute,
                    "--profile", absolute,
                    "--private-key", absolute,
                    "--key-id", "demo",
                    "--output", absolute,
                ],
                ("--product-root", "--profile", "--private-key", "--output"),
            ),
            (
                ["verify-bundle", "--bundle", absolute, "--trust-root", absolute],
                ("--bundle", "--trust-root"),
            ),
            (
                [
                    "prepare-models",
                    "--profile", absolute,
                    "--state-dir", absolute,
                    "--model-cache", absolute,
                ],
                ("--profile", "--state-dir", "--model-cache"),
            ),
            (
                ["model-status", "--profile", absolute, "--state-dir", absolute],
                ("--profile", "--state-dir"),
            ),
            (
                [
                    "query",
                    "--bundle", absolute,
                    "--trust-root", absolute,
                    "--state-dir", absolute,
                    "--intent", absolute,
                ],
                ("--bundle", "--trust-root", "--state-dir", "--intent"),
            ),
        )
        deps = CliDependencies(
            build_bundle=_unexpected_dependency,
            load_bundle=_unexpected_dependency,
            prepare_models=_unexpected_dependency,
            load_models=_unexpected_dependency,
            load_runtime=_unexpected_dependency,
            build_index=_unexpected_dependency,
            make_retriever=_unexpected_dependency,
            snapshot_download=_unexpected_dependency,
        )

        for arguments, options in cases:
            for option in options:
                with self.subTest(command=arguments[0], option=option):
                    invalid = list(arguments)
                    invalid[invalid.index(option) + 1] = "relative/path"
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as captured:
                            main(invalid, deps=deps)
                    self.assertEqual(2, captured.exception.code)
                    self.assertIn("must be absolute", stderr.getvalue())

    def test_repository_write_targets_fail_before_dependencies(self) -> None:
        """Repository targets and filesystem aliases must never receive state."""
        absolute = str(self.external_root / "external")
        cases = (
            (
                [
                    "build-bundle",
                    "--product-root", str(PRODUCT_ROOT),
                    "--profile", str(PROFILE_PATH),
                    "--private-key", absolute,
                    "--key-id", "demo",
                    "--output", absolute,
                ],
                "--output",
            ),
            (
                [
                    "prepare-models",
                    "--profile", str(PROFILE_PATH),
                    "--state-dir", absolute,
                    "--model-cache", absolute,
                ],
                "--state-dir",
            ),
            (
                [
                    "prepare-models",
                    "--profile", str(PROFILE_PATH),
                    "--state-dir", absolute,
                    "--model-cache", absolute,
                ],
                "--model-cache",
            ),
            (
                [
                    "model-status",
                    "--profile", str(PROFILE_PATH),
                    "--state-dir", absolute,
                ],
                "--state-dir",
            ),
            (
                [
                    "query",
                    "--bundle", absolute,
                    "--trust-root", absolute,
                    "--state-dir", absolute,
                    "--intent", absolute,
                ],
                "--state-dir",
            ),
        )
        protected_roots = (
            ROOT,
            ROOT / "src/zdecision",
            ROOT / "plugins/zdecision",
            ROOT / "decision-registry",
        )
        case_variant_root = (
            ROOT.parent.parent / ROOT.parent.name.upper() / ROOT.name
        )
        self.assertNotEqual(ROOT, case_variant_root)
        self.assertTrue(case_variant_root.samefile(ROOT))
        repository_link = self.external_root / "repository-link"
        repository_link.symlink_to(ROOT, target_is_directory=True)
        target_sets = (
            (
                "lexical",
                tuple(root / "generated-target" for root in protected_roots),
            ),
            (
                "symlink",
                tuple(
                    repository_link / root.relative_to(ROOT) / "generated-target"
                    for root in protected_roots
                ),
            ),
            (
                "case-variant",
                tuple(
                    case_variant_root
                    / root.relative_to(ROOT)
                    / "case-variant-new-target"
                    for root in protected_roots
                ),
            ),
        )
        for protected_root, target in zip(
            protected_roots, target_sets[2][1], strict=True
        ):
            self.assertTrue(target.parent.samefile(protected_root))
            self.assertFalse(target.exists())
        profile = _profile()
        bundle = SimpleNamespace(profile=profile)
        installed = InstalledModels(
            profile_digest=profile.digest,
            embedding_path=self.external_root / "embedding",
            reranker_path=self.external_root / "reranker",
            install_manifest_path=self.external_root / "model-install.json",
        )

        for arguments, option in cases:
            for target_kind, targets in target_sets:
                for target in targets:
                    calls: list[str] = []

                    def record_build(**kwargs):
                        calls.append("build")
                        return kwargs["output_root"]

                    def record_prepare(**kwargs):
                        calls.append("prepare")
                        return installed

                    candidate = list(arguments)
                    candidate[candidate.index(option) + 1] = str(target)
                    stderr = io.StringIO()
                    with self.subTest(
                        option=option,
                        target=target,
                        target_kind=target_kind,
                    ):
                        with contextlib.redirect_stderr(stderr):
                            try:
                                main(
                                    candidate,
                                    deps=replace(
                                        CliDependencies(),
                                        build_bundle=record_build,
                                        load_bundle=lambda **kwargs: (
                                            calls.append("bundle") or bundle
                                        ),
                                        prepare_models=record_prepare,
                                        load_models=lambda actual_profile, state_root: (
                                            calls.append("models") or installed
                                        ),
                                        load_runtime=lambda actual_profile, actual_installed: (
                                            calls.append("runtime") or object()
                                        ),
                                        build_index=lambda actual_bundle, runtime: (
                                            calls.append("index") or object()
                                        ),
                                        snapshot_download=lambda **kwargs: absolute,
                                    ),
                                )
                            except SystemExit as error:
                                exit_code = error.code
                            else:
                                exit_code = None
                        self.assertEqual(2, exit_code)
                        self.assertEqual([], calls)
                        self.assertIn("outside the repository", stderr.getvalue())

    def test_write_target_identity_failure_fails_before_dependencies(self) -> None:
        """An unreadable filesystem identity must fail closed during parsing."""
        calls: list[str] = []
        detail = "identity-inspection-detail"

        def record_build(**kwargs):
            calls.append("build")
            return kwargs["output_root"]

        stderr = io.StringIO()
        with (
            patch(
                "zdecision.recall.demo.cli.Path.stat",
                side_effect=OSError(detail),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as captured:
                main(
                    [
                        "build-bundle",
                        "--product-root", str(PRODUCT_ROOT),
                        "--profile", str(PROFILE_PATH),
                        "--private-key", str(self.external_root / "private.key"),
                        "--key-id", "demo",
                        "--output", str(self.external_root / "bundle"),
                    ],
                    deps=replace(CliDependencies(), build_bundle=record_build),
                )

        self.assertEqual(2, captured.exception.code)
        self.assertEqual([], calls)
        self.assertIn("filesystem path is invalid", stderr.getvalue())
        self.assertNotIn(detail, stderr.getvalue())

    def test_long_option_abbreviations_fail_before_dependencies(self) -> None:
        """An abbreviated option must never be accepted as a write/read boundary."""
        absolute = str(self.external_root / "external")
        commands = (
            ["--he"],
            [
                "build-bundle",
                "--product-r", str(PRODUCT_ROOT),
                "--profile", str(PROFILE_PATH),
                "--private-key", absolute,
                "--key-id", "demo",
                "--output", absolute,
            ],
            [
                "verify-bundle",
                "--bund", absolute,
                "--trust-root", absolute,
            ],
            [
                "prepare-models",
                "--profile", str(PROFILE_PATH),
                "--state-d", absolute,
                "--model-cache", absolute,
            ],
            [
                "model-status",
                "--profile", str(PROFILE_PATH),
                "--state-d", absolute,
            ],
            [
                "query",
                "--bundle", absolute,
                "--trust-r", absolute,
                "--state-dir", absolute,
                "--intent", absolute,
            ],
        )
        profile = _profile()
        bundle = SimpleNamespace(
            profile=profile,
            decision_space_id=PRODUCT_ID,
            product_name="third-party-services",
            repository="zstack-ui-next",
            decisions=tuple(range(10)),
            manifest_digest="a" * 64,
        )
        installed = InstalledModels(
            profile_digest=profile.digest,
            embedding_path=self.external_root / "embedding",
            reranker_path=self.external_root / "reranker",
            install_manifest_path=self.external_root / "model-install.json",
        )

        for command in commands:
            calls: list[str] = []

            def record(name, value):
                calls.append(name)
                return value

            deps = CliDependencies(
                build_bundle=lambda **kwargs: record("build", absolute),
                load_bundle=lambda **kwargs: record("bundle", bundle),
                prepare_models=lambda **kwargs: record("prepare", installed),
                load_models=lambda profile, state_root: record("models", installed),
                load_runtime=lambda profile, actual: record("runtime", object()),
                build_index=lambda actual_bundle, runtime: record("index", object()),
                make_retriever=lambda: record("retriever", object()),
                snapshot_download=lambda **kwargs: record("snapshot", absolute),
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.subTest(command=command[0]):
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    try:
                        main(command, deps=deps)
                    except SystemExit as error:
                        exit_code = error.code
                    else:
                        exit_code = None
                self.assertEqual(2, exit_code)
                self.assertEqual([], calls)
                self.assertIn("error:", stderr.getvalue())

    def test_build_and_verify_emit_only_safe_canonical_status(self) -> None:
        """A CLI change must not expose key bytes, key paths, or Decision text."""
        private_path, trust_root, private_bytes = _write_keys(self.external_root)
        bundle_root = self.external_root / "bundle"

        code, stdout, stderr = _capture(
            [
                "build-bundle",
                "--product-root", str(PRODUCT_ROOT),
                "--profile", str(PROFILE_PATH),
                "--private-key", str(private_path),
                "--key-id", "demo-key-1",
                "--output", str(bundle_root),
            ]
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(canonical_json_bytes({"status": "built"}).decode(), stdout)
        self.assertNotIn(str(private_path), stdout)
        self.assertNotIn(private_bytes, b"".join(path.read_bytes() for path in bundle_root.iterdir()))
        self.assertEqual(
            {"retrieval-profile.json", "signed-manifest.json", "snapshot.json"},
            {path.name for path in bundle_root.iterdir()},
        )

        code, stdout, stderr = _capture(
            [
                "verify-bundle",
                "--bundle", str(bundle_root),
                "--trust-root", str(trust_root),
            ]
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        status = json.loads(stdout)
        self.assertEqual(canonical_json_bytes(status).decode(), stdout)
        self.assertEqual(
            {
                "decision_count",
                "decision_space_id",
                "manifest_digest",
                "product_name",
                "profile_digest",
                "profile_id",
                "repository",
                "status",
            },
            set(status),
        )
        self.assertEqual("verified", status["status"])
        self.assertEqual(10, status["decision_count"])
        self.assertEqual(PRODUCT_ID, status["decision_space_id"])
        self.assertEqual("third-party-services", status["product_name"])
        self.assertEqual("zstack-ui-next", status["repository"])
        self.assertEqual(64, len(status["profile_digest"]))
        self.assertEqual(64, len(status["manifest_digest"]))
        self.assertNotIn(
            "后端授权 cleanupOwnResources 或 cleanupAnyResources",
            stdout,
        )

    def test_prepare_models_is_the_only_network_capable_command(self) -> None:
        """Moving Hub resolution outside preparation could make offline paths dial out."""
        profile = _profile()
        calls: list[dict[str, object]] = []
        prepared: list[tuple[DemoRetrievalProfile, Path]] = []

        def download(**kwargs):
            calls.append(kwargs)
            return str(self.external_root / f"snapshot-{len(calls)}")

        def prepare(*, profile, state_root, snapshot_resolver):
            prepared.append((profile, state_root))
            snapshot_resolver(profile.embedding.model_id, profile.embedding.revision)
            snapshot_resolver(profile.reranker.model_id, profile.reranker.revision)
            return InstalledModels(
                profile_digest=profile.digest,
                embedding_path=self.external_root / "installed/embedding",
                reranker_path=self.external_root / "installed/reranker",
                install_manifest_path=self.external_root / "installed/model-install.json",
            )

        deps = replace(
            CliDependencies(),
            prepare_models=prepare,
            snapshot_download=download,
        )
        state_root = self.external_root / "state"
        model_cache = self.external_root / "cache"

        code, stdout, stderr = _capture(
            [
                "prepare-models",
                "--profile", str(PROFILE_PATH),
                "--state-dir", str(state_root),
                "--model-cache", str(model_cache),
            ],
            deps=deps,
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual([(profile, state_root)], prepared)
        self.assertEqual(
            [
                {
                    "repo_id": profile.embedding.model_id,
                    "revision": profile.embedding.revision,
                    "cache_dir": str(model_cache),
                    "local_dir": None,
                },
                {
                    "repo_id": profile.reranker.model_id,
                    "revision": profile.reranker.revision,
                    "cache_dir": str(model_cache),
                    "local_dir": None,
                },
            ],
            calls,
        )
        status = json.loads(stdout)
        self.assertEqual(canonical_json_bytes(status).decode(), stdout)
        self.assertEqual("ready", status["status"])
        self.assertEqual(profile.digest, status["profile_digest"])
        self.assertNotIn(str(model_cache), stdout)
        self.assertNotIn("snapshot-", stdout)

    def test_default_snapshot_failure_is_sanitized(self) -> None:
        """A cache/download failure must not expose Hub or filesystem details."""
        secret = str(self.external_root / "cache/internal/blob")

        def fail_download(**kwargs):
            raise OSError(f"cache miss at {secret}")

        with patch.dict(
            sys.modules,
            {"huggingface_hub": SimpleNamespace(snapshot_download=fail_download)},
        ):
            code, stdout, stderr = _capture(
                [
                    "prepare-models",
                    "--profile", str(PROFILE_PATH),
                    "--state-dir", str(self.external_root / "state"),
                    "--model-cache", str(self.external_root / "cache"),
                ]
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            {"code": "model_snapshot_unavailable", "status": "error"},
            json.loads(stderr),
        )
        self.assertNotIn(secret, stderr)

    def test_model_status_revalidates_bytes_without_loading_runtime(self) -> None:
        """A modified model byte must report not-ready before Torch can load it."""
        committed_profile = _profile()
        cache_root = self.external_root / "cache"
        snapshots = {
            committed_profile.embedding.model_id: _make_snapshot(cache_root, "embedding"),
            committed_profile.reranker.model_id: _make_snapshot(cache_root, "reranker"),
        }
        profile = _profile_for_snapshots(snapshots)
        profile_path = self.external_root / "tiny-profile.json"
        profile_path.write_bytes(canonical_json_bytes(profile.to_dict()))
        state_root = self.external_root / "state"
        installed = _prepare_tiny(
            profile=profile,
            state_root=state_root,
            snapshot_resolver=lambda model_id, revision: snapshots[model_id],
        )
        deps = replace(CliDependencies(), load_runtime=_unexpected_dependency)

        code, stdout, stderr = _capture(
            [
                "model-status",
                "--profile", str(profile_path),
                "--state-dir", str(state_root),
            ],
            deps=deps,
        )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual("ready", json.loads(stdout)["status"])

        changed = installed.embedding_path / "config.json"
        changed.chmod(0o600)
        changed.write_bytes(b"changed\n")
        changed.chmod(0o400)
        code, stdout, stderr = _capture(
            [
                "model-status",
                "--profile", str(profile_path),
                "--state-dir", str(state_root),
            ],
            deps=deps,
        )
        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertEqual(
            canonical_json_bytes(
                {"code": "installed_file_digest_invalid", "status": "not-ready"}
            ).decode(),
            stdout,
        )

    def test_non_runtime_commands_do_not_import_huggingface_hub(self) -> None:
        """Bundle commands and model status must not need the Hub package."""
        profile = _profile()
        fake_bundle = SimpleNamespace(
            decision_space_id=PRODUCT_ID,
            product_name="third-party-services",
            repository="zstack-ui-next",
            decisions=tuple(range(10)),
            profile=profile,
            manifest_digest="a" * 64,
        )
        installed = InstalledModels(
            profile_digest=profile.digest,
            embedding_path=self.external_root / "embedding",
            reranker_path=self.external_root / "reranker",
            install_manifest_path=self.external_root / "model-install.json",
        )
        deps = replace(
            CliDependencies(),
            build_bundle=lambda **kwargs: self.external_root / "bundle",
            load_bundle=lambda **kwargs: fake_bundle,
            load_models=lambda profile, state_root: installed,
        )
        commands = (
            [
                "build-bundle",
                "--product-root", str(PRODUCT_ROOT),
                "--profile", str(PROFILE_PATH),
                "--private-key", str(self.external_root / "private.key"),
                "--key-id", "demo",
                "--output", str(self.external_root / "bundle"),
            ],
            [
                "verify-bundle",
                "--bundle", str(self.external_root / "bundle"),
                "--trust-root", str(self.external_root / "trust.pub"),
            ],
            [
                "model-status",
                "--profile", str(PROFILE_PATH),
                "--state-dir", str(self.external_root / "state"),
            ],
        )

        with patch.dict(sys.modules, {"huggingface_hub": None}):
            for command in commands:
                with self.subTest(command=command[0]):
                    code, _, _ = _capture(command, deps=deps)
                    self.assertEqual(0, code)

    def test_query_preserves_trust_order_and_prints_complete_decisions(self) -> None:
        """Reordering trust checks or trimming formal Decisions must break this result."""
        profile = _profile()
        revision = _cleanup_revision()
        intent_path = _write_intent(self.external_root)
        expected_intent = RecallIntent.from_dict(_intent_value())
        self.assertEqual(
            ("SecurityServiceInstance", "cleanupOwnResources", "cleanupAnyResources"),
            expected_intent.domain_objects,
        )
        self.assertEqual(
            (
                "packages/products/third-party-services/apps/security-services/src/api/"
                "security-instance-actions.ts",
            ),
            expected_intent.repository_relative_paths,
        )
        self.assertEqual(
            ("不修改清理失败重试流程",),
            expected_intent.exclusions,
        )
        bundle = SimpleNamespace(
            decision_space_id=PRODUCT_ID,
            product_name="third-party-services",
            repository="zstack-ui-next",
            decisions=(revision,),
            profile=profile,
            manifest_digest="b" * 64,
        )
        installed = SimpleNamespace(profile_digest=profile.digest)
        runtime = object()
        index = object()
        events: list[str] = []
        resolver_calls: list[dict[str, object]] = []

        def load_bundle(*, bundle_root, trust_root_path):
            events.append("verify-bundle")
            self.assertEqual(self.external_root / "bundle", bundle_root)
            self.assertEqual(self.external_root / "trust.pub", trust_root_path)
            return bundle

        def load_models(actual_profile, state_root):
            events.append("load-models")
            self.assertEqual(profile, actual_profile)
            self.assertEqual(self.external_root / "state", state_root)
            return installed

        def load_runtime(actual_profile, actual_installed):
            events.append("load-runtime")
            self.assertIs(profile, actual_profile)
            self.assertIs(installed, actual_installed)
            return runtime

        def build_index(actual_bundle, actual_runtime):
            events.append("build-index")
            self.assertIs(bundle, actual_bundle)
            self.assertIs(runtime, actual_runtime)
            return index

        class Retriever:
            def retrieve(inner_self, intent, actual_bundle, actual_index, actual_runtime):
                events.append("retrieve")
                self.assertEqual(expected_intent, intent)
                self.assertIs(bundle, actual_bundle)
                self.assertIs(index, actual_index)
                self.assertIs(runtime, actual_runtime)
                return DemoRecallResult(
                    intent_digest=intent.digest,
                    profile_digest=profile.digest,
                    manifest_digest=bundle.manifest_digest,
                    items=(
                        RankedDemoDecision(
                            revision=revision,
                            digest="c" * 64,
                            reranker_score=0.75,
                            fused_score=0.05,
                            match_reason="semantic+lexical+path",
                        ),
                    ),
                )

        deps = replace(
            CliDependencies(),
            load_bundle=load_bundle,
            load_models=load_models,
            load_runtime=load_runtime,
            build_index=build_index,
            make_retriever=Retriever,
            snapshot_download=lambda **kwargs: resolver_calls.append(kwargs),
        )

        code, stdout, stderr = _capture(
            [
                "query",
                "--bundle", str(self.external_root / "bundle"),
                "--trust-root", str(self.external_root / "trust.pub"),
                "--state-dir", str(self.external_root / "state"),
                "--intent", str(intent_path),
            ],
            deps=deps,
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(
            ["verify-bundle", "load-models", "load-runtime", "build-index", "retrieve"],
            events,
        )
        self.assertEqual([], resolver_calls)
        output = json.loads(stdout)
        self.assertEqual(canonical_json_bytes(output).decode(), stdout)
        self.assertEqual(revision.to_dict(), output["items"][0]["decision"])
        self.assertEqual(
            CLEANUP_REVISION_PATH.read_bytes(),
            canonical_json_bytes(output["items"][0]["decision"]),
        )
        self.assertEqual("semantic+lexical+path", output["items"][0]["match_reason"])
        self.assertEqual(0.75, output["items"][0]["reranker_score"])
        self.assertEqual(0.05, output["items"][0]["fused_score"])

    def test_query_fails_closed_at_bundle_and_model_boundaries(self) -> None:
        """Bundle/model failures must stop before runtime loading with no fallback."""
        intent_path = _write_intent(self.external_root)
        query = [
            "query",
            "--bundle", str(self.external_root / "bundle"),
            "--trust-root", str(self.external_root / "trust.pub"),
            "--state-dir", str(self.external_root / "state"),
            "--intent", str(intent_path),
        ]
        profile = _profile()
        bundle = SimpleNamespace(profile=profile)

        bundle_events: list[str] = []

        def corrupt_bundle(**kwargs):
            bundle_events.append("verify-bundle")
            raise DemoBundleError("bundle_invalid")

        code, stdout, stderr = _capture(
            query,
            deps=replace(
                CliDependencies(),
                load_bundle=corrupt_bundle,
                load_models=_unexpected_dependency,
                load_runtime=_unexpected_dependency,
                snapshot_download=_unexpected_dependency,
            ),
        )
        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(["verify-bundle"], bundle_events)
        self.assertEqual(
            {"code": "bundle_invalid", "status": "error"}, json.loads(stderr)
        )

        model_events: list[str] = []

        def verified_bundle(**kwargs):
            model_events.append("verify-bundle")
            return bundle

        def corrupt_models(actual_profile, state_root):
            model_events.append("load-models")
            raise ModelStoreError("installed_file_digest_invalid")

        code, stdout, stderr = _capture(
            query,
            deps=replace(
                CliDependencies(),
                load_bundle=verified_bundle,
                load_models=corrupt_models,
                load_runtime=_unexpected_dependency,
                snapshot_download=_unexpected_dependency,
            ),
        )
        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(["verify-bundle", "load-models"], model_events)
        self.assertEqual(
            {"code": "installed_file_digest_invalid", "status": "error"},
            json.loads(stderr),
        )
        self.assertNotIn(str(self.external_root), stderr)

    def test_query_builds_index_before_parsing_intent_and_never_falls_back(self) -> None:
        """Invalid input and reranker failure must not cause keyword-only retrieval."""
        profile = _profile()
        bundle = SimpleNamespace(profile=profile)
        installed = object()
        runtime = object()
        index = object()
        events: list[str] = []
        invalid_intent = _write_intent(self.external_root, {"secret": "do-not-leak"})

        deps = replace(
            CliDependencies(),
            load_bundle=lambda **kwargs: events.append("verify-bundle") or bundle,
            load_models=lambda actual_profile, state_root: events.append("load-models") or installed,
            load_runtime=lambda actual_profile, actual_installed: events.append("load-runtime") or runtime,
            build_index=lambda actual_bundle, actual_runtime: events.append("build-index") or index,
            make_retriever=_unexpected_dependency,
            snapshot_download=_unexpected_dependency,
        )
        query = [
            "query",
            "--bundle", str(self.external_root / "bundle"),
            "--trust-root", str(self.external_root / "trust.pub"),
            "--state-dir", str(self.external_root / "state"),
            "--intent", str(invalid_intent),
        ]

        code, stdout, stderr = _capture(query, deps=deps)

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            ["verify-bundle", "load-models", "load-runtime", "build-index"],
            events,
        )
        self.assertEqual({"code": "intent_invalid", "status": "error"}, json.loads(stderr))
        self.assertNotIn("do-not-leak", stderr)

        _write_intent(self.external_root)
        retrieval_calls = []

        def fail_retrieval(intent, actual_bundle, actual_index, actual_runtime):
            retrieval_calls.append(intent)
            raise DemoRetrievalError("reranker_output_invalid")

        code, stdout, stderr = _capture(
            query,
            deps=replace(
                deps,
                make_retriever=lambda: SimpleNamespace(retrieve=fail_retrieval),
            ),
        )
        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(1, len(retrieval_calls))
        self.assertEqual(
            {"code": "reranker_output_invalid", "status": "error"},
            json.loads(stderr),
        )

    def test_query_sanitizes_known_failures_but_exposes_development_bugs(self) -> None:
        """Only the prototype's explicit error contracts may be collapsed to codes."""
        intent_path = _write_intent(self.external_root)
        query = [
            "query",
            "--bundle", str(self.external_root / "bundle"),
            "--trust-root", str(self.external_root / "trust.pub"),
            "--state-dir", str(self.external_root / "state"),
            "--intent", str(intent_path),
        ]
        profile = _profile()
        bundle = SimpleNamespace(profile=profile)
        installed = object()

        for failure in (
            DemoRuntimeError("runtime_load_failed"),
            DemoIndexError("embedding_output_invalid"),
        ):
            with self.subTest(code=failure.code):
                if isinstance(failure, DemoRuntimeError):
                    deps = replace(
                        CliDependencies(),
                        load_bundle=lambda **kwargs: bundle,
                        load_models=lambda actual_profile, state_root: installed,
                        load_runtime=lambda actual_profile, actual_installed, error=failure: _raise(error),
                        snapshot_download=_unexpected_dependency,
                    )
                else:
                    deps = replace(
                        CliDependencies(),
                        load_bundle=lambda **kwargs: bundle,
                        load_models=lambda actual_profile, state_root: installed,
                        load_runtime=lambda actual_profile, actual_installed: object(),
                        build_index=lambda actual_bundle, actual_runtime, error=failure: _raise(error),
                        snapshot_download=_unexpected_dependency,
                    )
                code, stdout, stderr = _capture(query, deps=deps)
                self.assertEqual(1, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    {"code": failure.code, "status": "error"}, json.loads(stderr)
                )

        deps = replace(
            CliDependencies(),
            load_bundle=lambda **kwargs: _raise(RuntimeError("visible-development-bug")),
        )
        with self.assertRaisesRegex(RuntimeError, "visible-development-bug"):
            _capture(query, deps=deps)

    def test_default_runtime_load_failure_is_sanitized(self) -> None:
        """A local Transformers load failure must not expose model paths or internals."""
        committed_profile = _profile()
        cache_root = self.external_root / "cache"
        snapshots = {
            committed_profile.embedding.model_id: _make_snapshot(cache_root, "embedding"),
            committed_profile.reranker.model_id: _make_snapshot(cache_root, "reranker"),
        }
        profile = _profile_for_snapshots(snapshots)
        state_root = self.external_root / "state"
        _prepare_tiny(
            profile=profile,
            state_root=state_root,
            snapshot_resolver=lambda model_id, revision: snapshots[model_id],
        )
        bundle = SimpleNamespace(profile=profile)
        intent_path = _write_intent(self.external_root)
        secret = str(self.external_root / "embedding/config.json")

        with patch(
            "transformers.AutoTokenizer.from_pretrained",
            side_effect=OSError(f"failed at {secret}"),
        ):
            code, stdout, stderr = _capture(
                [
                    "query",
                    "--bundle", str(self.external_root / "bundle"),
                    "--trust-root", str(self.external_root / "trust.pub"),
                    "--state-dir", str(state_root),
                    "--intent", str(intent_path),
                ],
                deps=replace(
                    CliDependencies(),
                    load_bundle=lambda **kwargs: bundle,
                    build_index=_unexpected_dependency,
                    snapshot_download=_unexpected_dependency,
                ),
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            {"code": "runtime_load_failed", "status": "error"},
            json.loads(stderr),
        )
        self.assertNotIn(secret, stderr)

    def test_default_runtime_wrapper_exposes_development_errors(self) -> None:
        """The CLI wrapper must not classify failures from the whole loader seam."""
        committed_profile = _profile()
        cache_root = self.external_root / "cache"
        snapshots = {
            committed_profile.embedding.model_id: _make_snapshot(cache_root, "embedding"),
            committed_profile.reranker.model_id: _make_snapshot(cache_root, "reranker"),
        }
        profile = _profile_for_snapshots(snapshots)
        state_root = self.external_root / "state"
        _prepare_tiny(
            profile=profile,
            state_root=state_root,
            snapshot_resolver=lambda model_id, revision: snapshots[model_id],
        )
        bundle = SimpleNamespace(profile=profile)
        intent_path = _write_intent(self.external_root)
        query = [
            "query",
            "--bundle", str(self.external_root / "bundle"),
            "--trust-root", str(self.external_root / "trust.pub"),
            "--state-dir", str(state_root),
            "--intent", str(intent_path),
        ]
        deps = replace(
            CliDependencies(),
            load_bundle=lambda **kwargs: bundle,
            build_index=_unexpected_dependency,
            snapshot_download=_unexpected_dependency,
        )

        for error in (
            RuntimeError("runtime-development-bug"),
            ValueError("value-development-bug"),
            OSError("os-development-bug"),
            ImportError("import-development-bug"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch(
                    "zdecision.recall.demo.cli.load_transformers_runtime",
                    side_effect=error,
                ):
                    with self.assertRaisesRegex(type(error), str(error)):
                        _capture(query, deps=deps)

    def test_default_runtime_wrapper_emits_domain_error(self) -> None:
        """The CLI must canonically emit an error already classified by runtime."""
        committed_profile = _profile()
        cache_root = self.external_root / "cache"
        snapshots = {
            committed_profile.embedding.model_id: _make_snapshot(cache_root, "embedding"),
            committed_profile.reranker.model_id: _make_snapshot(cache_root, "reranker"),
        }
        profile = _profile_for_snapshots(snapshots)
        state_root = self.external_root / "state"
        _prepare_tiny(
            profile=profile,
            state_root=state_root,
            snapshot_resolver=lambda model_id, revision: snapshots[model_id],
        )
        bundle = SimpleNamespace(profile=profile)
        intent_path = _write_intent(self.external_root)

        with patch(
            "zdecision.recall.demo.cli.load_transformers_runtime",
            side_effect=DemoRuntimeError("runtime_load_failed"),
        ):
            code, stdout, stderr = _capture(
                [
                    "query",
                    "--bundle", str(self.external_root / "bundle"),
                    "--trust-root", str(self.external_root / "trust.pub"),
                    "--state-dir", str(state_root),
                    "--intent", str(intent_path),
                ],
                deps=replace(
                    CliDependencies(),
                    load_bundle=lambda **kwargs: bundle,
                    build_index=_unexpected_dependency,
                    snapshot_download=_unexpected_dependency,
                ),
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            {"code": "runtime_load_failed", "status": "error"},
            json.loads(stderr),
        )

    def test_commands_do_not_mutate_product_source_or_production_code(self) -> None:
        """Standalone commands may write external state, never source or Registry files."""
        protected_roots = (
            ROOT / "src/zdecision",
            ROOT / "plugins/zdecision",
            ROOT / "decision-registry",
        )
        before = {root: _tree_fingerprint(root) for root in protected_roots}
        private_path, trust_root, _ = _write_keys(self.external_root)
        bundle_root = self.external_root / "bundle"
        state_root = self.external_root / "state"
        committed_profile = _profile()
        cache_root = self.external_root / "cache"
        snapshots = {
            committed_profile.embedding.model_id: _make_snapshot(cache_root, "embedding"),
            committed_profile.reranker.model_id: _make_snapshot(cache_root, "reranker"),
        }
        profile = _profile_for_snapshots(snapshots)
        profile_path = self.external_root / "tiny-profile.json"
        profile_path.write_bytes(canonical_json_bytes(profile.to_dict()))

        self.assertEqual(
            0,
            _capture(
                [
                    "build-bundle",
                    "--product-root", str(PRODUCT_ROOT),
                    "--profile", str(profile_path),
                    "--private-key", str(private_path),
                    "--key-id", "demo-key",
                    "--output", str(bundle_root),
                ]
            )[0],
        )
        self.assertEqual(
            0,
            _capture(
                [
                    "verify-bundle",
                    "--bundle", str(bundle_root),
                    "--trust-root", str(trust_root),
                ]
            )[0],
        )
        self.assertEqual(
            0,
            _capture(
                [
                    "prepare-models",
                    "--profile", str(profile_path),
                    "--state-dir", str(state_root),
                    "--model-cache", str(cache_root),
                ],
                deps=replace(
                    CliDependencies(),
                    prepare_models=_prepare_tiny,
                    snapshot_download=lambda **kwargs: str(snapshots[kwargs["repo_id"]]),
                ),
            )[0],
        )
        self.assertEqual(
            0,
            _capture(
                [
                    "model-status",
                    "--profile", str(profile_path),
                    "--state-dir", str(state_root),
                ]
            )[0],
        )
        intent_path = _write_intent(self.external_root)
        fake_runtime = object()
        fake_index = object()
        query_deps = replace(
            CliDependencies(),
            load_runtime=lambda actual_profile, installed: fake_runtime,
            build_index=lambda bundle, runtime: fake_index,
            make_retriever=lambda: SimpleNamespace(
                retrieve=lambda intent, bundle, index, runtime: DemoRecallResult(
                    intent_digest=intent.digest,
                    profile_digest=bundle.profile.digest,
                    manifest_digest=bundle.manifest_digest,
                    items=(),
                )
            ),
            snapshot_download=_unexpected_dependency,
        )
        self.assertEqual(
            0,
            _capture(
                [
                    "query",
                    "--bundle", str(bundle_root),
                    "--trust-root", str(trust_root),
                    "--state-dir", str(state_root),
                    "--intent", str(intent_path),
                ],
                deps=query_deps,
            )[0],
        )

        self.assertEqual(
            before,
            {root: _tree_fingerprint(root) for root in protected_roots},
        )


if __name__ == "__main__":
    unittest.main()
