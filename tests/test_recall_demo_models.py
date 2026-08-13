"""Local model preparation and offline runtime contracts for the recall demo."""

from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import torch
from safetensors import SafetensorError

from zdecision.jsonio import canonical_json_bytes

from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.model_store import (
    InstalledModels,
    ModelStoreError,
    _clone_file,
    _fsync_directory,
    _seal_directory,
    load_installed_model_metadata,
    load_installed_models,
    prepare_models,
)
from zdecision.recall.demo.runtime import (
    DemoRuntimeError,
    ModelRuntimeBundle,
    load_transformers_runtime,
)


PROFILE_PATH = Path(__file__).parents[1] / "src/zdecision/recall/demo/demo-profile.json"
REQUIRED_FILES = (
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)

_DARWIN_ACL_TYPE_EXTENDED = 0x00000100
_DARWIN_INHERITABLE_ACL = (
    b"!#acl 1\n"
    b"group:ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C:everyone:12:"
    b"allow,file_inherit,directory_inherit:read,write,execute\n"
)
_DARWIN_FILE_ACL = (
    b"!#acl 1\n"
    b"group:ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C:everyone:12:allow:read\n"
)


def _darwin_acl_libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_from_text.argtypes = [ctypes.c_char_p]
    libc.acl_from_text.restype = ctypes.c_void_p
    libc.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libc.acl_set_fd_np.restype = ctypes.c_int
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free.restype = ctypes.c_int
    return libc


def _darwin_open_path(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if path.is_dir():
        flags |= getattr(os, "O_DIRECTORY", 0)
    return os.open(path, flags)


def _darwin_set_acl(path: Path, text: bytes) -> None:
    libc = _darwin_acl_libc()
    ctypes.set_errno(0)
    acl = libc.acl_from_text(text)
    if acl is None:
        raise OSError(ctypes.get_errno(), "acl_from_text")
    descriptor = _darwin_open_path(path)
    try:
        if libc.acl_set_fd_np(descriptor, acl, _DARWIN_ACL_TYPE_EXTENDED) != 0:
            raise OSError(ctypes.get_errno(), "acl_set_fd_np")
    finally:
        os.close(descriptor)
        if libc.acl_free(acl) != 0:
            raise OSError(ctypes.get_errno(), "acl_free")


def _darwin_has_acl(path: Path) -> bool:
    libc = _darwin_acl_libc()
    descriptor = _darwin_open_path(path)
    try:
        ctypes.set_errno(0)
        acl = libc.acl_get_fd_np(descriptor, _DARWIN_ACL_TYPE_EXTENDED)
        error_number = ctypes.get_errno()
    finally:
        os.close(descriptor)
    if acl is not None:
        if libc.acl_free(acl) != 0:
            raise OSError(ctypes.get_errno(), "acl_free")
        return True
    if error_number != errno.ENOENT:
        raise OSError(error_number, "acl_get_fd_np")
    return False


def _profile() -> DemoRetrievalProfile:
    return DemoRetrievalProfile.from_dict(
        json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    )


def _profile_for_snapshots(
    snapshots: dict[str, Path],
) -> DemoRetrievalProfile:
    value = copy.deepcopy(json.loads(PROFILE_PATH.read_text(encoding="utf-8")))
    for role in ("embedding", "reranker"):
        snapshot = snapshots[value[role]["model_id"]]
        value[role]["files"] = {
            name: {
                "sha256": hashlib.sha256((snapshot / name).read_bytes()).hexdigest(),
                "size": (snapshot / name).stat().st_size,
            }
            for name in REQUIRED_FILES
        }
    return DemoRetrievalProfile.from_dict(value)


def _make_snapshot(root: Path, role: str) -> Path:
    snapshot = root / role
    snapshot.mkdir(parents=True)
    for name in REQUIRED_FILES:
        (snapshot / name).write_bytes(f"{role}:{name}\n".encode())
    return snapshot


def _copy_clone(source_fd: int, destination_dir_fd: int, name: str) -> None:
    """Test-only distinct-inode cloner; production must use a forced CoW clone."""
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


def _make_tree_writable(root: Path) -> None:
    """Restore only a test-owned temporary tree so cleanup can remove sealed state."""
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


def _prepare_tiny(
    *,
    profile: DemoRetrievalProfile,
    state_root: Path,
    snapshot_resolver,
) -> InstalledModels:
    with patch(
        "zdecision.recall.demo.model_store._clone_file",
        side_effect=_copy_clone,
    ):
        return prepare_models(
            profile=profile,
            state_root=state_root,
            snapshot_resolver=snapshot_resolver,
        )


class ModelStoreTests(unittest.TestCase):
    """Prepared models are signed, independent CoW state files."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.sources_root = self.root / "cache"
        committed_profile = _profile()
        self.snapshots = {
            committed_profile.embedding.model_id: _make_snapshot(
                self.sources_root, "embedding"
            ),
            committed_profile.reranker.model_id: _make_snapshot(
                self.sources_root, "reranker"
            ),
        }
        self.profile = _profile_for_snapshots(self.snapshots)
        self.calls: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        _make_tree_writable(self.root)
        self.temporary.cleanup()

    def _resolver(self, model_id: str, revision: str) -> Path:
        self.calls.append((model_id, revision))
        return self.snapshots[model_id]

    def _prepare(self) -> InstalledModels:
        return _prepare_tiny(
            profile=self.profile,
            state_root=self.state_root,
            snapshot_resolver=self._resolver,
        )

    def _assert_error(self, code: str, action) -> None:
        with self.assertRaises(ModelStoreError) as captured:
            action()
        self.assertEqual(code, captured.exception.code)
        self.assertEqual(code, str(captured.exception))
        self.assertNotIn(str(self.root), str(captured.exception))

    def test_prepare_materializes_exact_verified_clones_and_loads_them(self) -> None:
        """Aliasing, omitting, or reordering a prepared blob must fail this contract."""
        source_state = {
            path.relative_to(self.sources_root).as_posix(): (
                path.read_bytes(),
                path.stat().st_ino,
            )
            for path in sorted(self.sources_root.rglob("*"))
            if path.is_file()
        }

        installed = self._prepare()

        self.assertEqual(
            [
                (
                    "intfloat/multilingual-e5-small",
                    "614241f622f53c4eeff9890bdc4f31cfecc418b3",
                ),
                (
                    "BAAI/bge-reranker-base",
                    "2cfc18c9415c912f9d8155881c133215df768a70",
                ),
            ],
            self.calls,
        )
        self.assertTrue(installed.embedding_path.is_dir())
        self.assertTrue(installed.reranker_path.is_dir())
        self.assertEqual(self.profile.digest, installed.profile_digest)
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))
        self.assertEqual(
            source_state,
            {
                path.relative_to(self.sources_root).as_posix(): (
                    path.read_bytes(),
                    path.stat().st_ino,
                )
                for path in sorted(self.sources_root.rglob("*"))
                if path.is_file()
            },
        )
        for role, destination_root, model_id in (
            ("embedding", installed.embedding_path, self.profile.embedding.model_id),
            ("reranker", installed.reranker_path, self.profile.reranker.model_id),
        ):
            self.assertEqual(
                set(REQUIRED_FILES), {path.name for path in destination_root.iterdir()}
            )
            for name in REQUIRED_FILES:
                destination = destination_root / name
                self.assertTrue(stat.S_ISREG(destination.lstat().st_mode))
                self.assertFalse(destination.is_symlink())
                self.assertNotEqual(
                    (
                        (self.snapshots[model_id] / name).stat().st_dev,
                        (self.snapshots[model_id] / name).stat().st_ino,
                    ),
                    (destination.stat().st_dev, destination.stat().st_ino),
                )

    def test_metadata_rejects_missing_declared_model_file_before_weight_load(self) -> None:
        """A preflight-ready installation must contain every sealed declared file."""
        installed = self._prepare()
        installed.embedding_path.chmod(0o700)
        (installed.embedding_path / "config.json").chmod(0o600)
        (installed.embedding_path / "config.json").unlink()
        installed.embedding_path.chmod(0o500)

        self._assert_error(
            "installed_file_set_invalid",
            lambda: load_installed_model_metadata(self.profile, self.state_root),
        )

        manifest_bytes = installed.install_manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        self.assertEqual(canonical_json_bytes(manifest), manifest_bytes)
        expected_files = []
        for role, model_id in (
            ("embedding", self.profile.embedding.model_id),
            ("reranker", self.profile.reranker.model_id),
        ):
            for name in REQUIRED_FILES:
                content = (self.snapshots[model_id] / name).read_bytes()
                expected_files.append(
                    {
                        "path": f"{role}/{name}",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                )
        expected_files.sort(key=lambda item: item["path"])
        self.assertEqual(2, manifest["schema_version"])
        self.assertEqual("cow-clone-v1", manifest["materialization"])
        self.assertEqual(expected_files, manifest["files"])
        self.assertFalse(
            any(path.suffix == ".tmp" for path in self.state_root.rglob("*"))
        )

    def test_prepare_rejects_relative_state_root(self) -> None:
        """A relative state root could place model bytes inside an unintended tree."""
        self._assert_error(
            "state_root_invalid",
            lambda: prepare_models(
                profile=self.profile,
                state_root=Path("relative-state"),
                snapshot_resolver=self._resolver,
            ),
        )
        self.assertEqual([], self.calls)

    def test_prepare_rejects_ancestor_symlinks_escaping_state_root(self) -> None:
        """A redirected demo/model ancestor must not receive prepared state files."""
        for redirected_name in ("recall-demo", "recall-demo/models"):
            with self.subTest(redirected_name=redirected_name):
                case_root = self.root / redirected_name.replace("/", "-")
                state_root = case_root / "state"
                outside = case_root / "outside"
                state_root.mkdir(parents=True)
                outside.mkdir()
                redirected_path = state_root / redirected_name
                redirected_path.parent.mkdir(parents=True, exist_ok=True)
                outside_target = outside / Path(redirected_name).name
                outside_target.mkdir()
                redirected_path.symlink_to(outside_target, target_is_directory=True)
                outside_before = tuple(outside.rglob("*"))
                calls_before = tuple(self.calls)

                self._assert_error(
                    "state_root_invalid",
                    lambda: prepare_models(
                        profile=self.profile,
                        state_root=state_root,
                        snapshot_resolver=self._resolver,
                    ),
                )

                self.assertEqual(calls_before, tuple(self.calls))
                self.assertEqual(outside_before, tuple(outside.rglob("*")))

    def test_prepare_rejects_empty_source_snapshot(self) -> None:
        """An empty cache snapshot must not become an active installation."""
        empty = self.root / "empty"
        empty.mkdir()
        self.snapshots[self.profile.embedding.model_id] = empty

        self._assert_error("source_snapshot_invalid", self._prepare)
        self.assertFalse((self.state_root / "recall-demo/models/current.json").exists())

    def test_prepare_rejects_source_symlink_not_resolving_to_regular_file(self) -> None:
        """A dangling cache link must not enter state as a claimed model file."""
        target = self.snapshots[self.profile.embedding.model_id] / "model.safetensors"
        target.unlink()
        target.symlink_to(self.root / "missing-blob")

        self._assert_error("source_file_invalid", self._prepare)
        self.assertFalse((self.state_root / "recall-demo/models/current.json").exists())

    def test_prepare_fails_closed_when_clone_is_unavailable(self) -> None:
        """Unsupported clones must leave no pointer, staging bytes, or fallback alias."""
        for error_number in (errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP):
            with self.subTest(error_number=error_number):
                state_root = self.root / f"state-{error_number}"
                link_counts = {
                    path: path.stat().st_nlink
                    for path in self.sources_root.rglob("*")
                    if path.is_file()
                }
                with patch(
                    "zdecision.recall.demo.model_store._clone_file",
                    side_effect=OSError(error_number, "clone unavailable"),
                    create=True,
                ):
                    self._assert_error(
                        "model_clone_unavailable",
                        lambda: prepare_models(
                            profile=self.profile,
                            state_root=state_root,
                            snapshot_resolver=self._resolver,
                        ),
                    )
                self.assertFalse(
                    (state_root / "recall-demo/models/current.json").exists()
                )
                staging = state_root / "recall-demo/models/staging"
                self.assertEqual((), tuple(staging.iterdir()))
                self.assertEqual(
                    link_counts,
                    {path: path.stat().st_nlink for path in link_counts},
                )

        unsupported_state = self.root / "state-unsupported"
        with patch(
            "zdecision.recall.demo.model_store.sys.platform", "unsupported"
        ):
            self._assert_error(
                "model_clone_unavailable",
                lambda: prepare_models(
                    profile=self.profile,
                    state_root=unsupported_state,
                    snapshot_resolver=self._resolver,
                ),
            )
        self.assertFalse(
            (unsupported_state / "recall-demo/models/current.json").exists()
        )
        unsupported_staging = unsupported_state / "recall-demo/models/staging"
        if unsupported_staging.exists():
            self.assertEqual((), tuple(unsupported_staging.iterdir()))

    def test_linux_clone_uses_the_running_machine_ioctl_abi(self) -> None:
        """MIPS, PowerPC, and SPARC must not receive the generic ioctl number."""
        cases = (
            ("x86_64", 0x40049409),
            ("aarch64", 0x40049409),
            ("riscv64", 0x40049409),
            ("s390x", 0x40049409),
            ("loongarch64", 0x40049409),
            ("mips64el", 0x80049409),
            ("ppc64le", 0x80049409),
            ("sparc64", 0x80049409),
        )
        source = self.snapshots[self.profile.embedding.model_id] / "config.json"
        source_fd = os.open(source, os.O_RDONLY)
        destination_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, (machine, expected_request) in enumerate(cases):
                with self.subTest(machine=machine):
                    with (
                        patch(
                            "zdecision.recall.demo.model_store.sys.platform",
                            "linux",
                        ),
                        patch("platform.machine", return_value=machine),
                        patch(
                            "zdecision.recall.demo.model_store.fcntl.ioctl"
                        ) as ioctl,
                    ):
                        _clone_file(source_fd, destination_fd, f"clone-{index}")
                    self.assertEqual(
                        (source_fd,),
                        ioctl.call_args.args[2:],
                    )
                    self.assertEqual(expected_request, ioctl.call_args.args[1])
        finally:
            os.close(destination_fd)
            os.close(source_fd)

    def test_linux_unknown_machine_fails_before_destination_creation(self) -> None:
        """An unverified ioctl ABI must not leave an empty claimed clone behind."""
        source = self.snapshots[self.profile.embedding.model_id] / "config.json"
        source_fd = os.open(source, os.O_RDONLY)
        destination_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        destination = self.root / "unknown-machine-clone"
        try:
            with (
                patch("zdecision.recall.demo.model_store.sys.platform", "linux"),
                patch("platform.machine", return_value="future-linux-abi"),
                patch(
                    "zdecision.recall.demo.model_store.fcntl.ioctl"
                ) as ioctl,
            ):
                with self.assertRaises(OSError) as captured:
                    _clone_file(source_fd, destination_fd, destination.name)
            self.assertEqual(errno.ENOTSUP, captured.exception.errno)
            self.assertFalse(destination.exists())
            ioctl.assert_not_called()
        finally:
            os.close(destination_fd)
            os.close(source_fd)

    def test_prepare_rejects_pre_polluted_source_before_activation(self) -> None:
        """Resolver location cannot redefine a signed model-byte expectation."""
        source = self.snapshots[self.profile.embedding.model_id] / "config.json"
        source.write_bytes(b"polluted\n")

        self._assert_error("source_file_digest_invalid", self._prepare)

        self.assertFalse((self.state_root / "recall-demo/models/current.json").exists())
        self.assertEqual(
            (), tuple((self.state_root / "recall-demo/models/staging").iterdir())
        )

    def test_prepare_rejects_alias_before_chmod_can_touch_source(self) -> None:
        """A malicious aliasing cloner must not make the cache file read-only."""
        source = self.snapshots[self.profile.embedding.model_id] / "config.json"
        source_mode = stat.S_IMODE(source.stat().st_mode)

        def hardlink_alias(_source_fd: int, destination_dir_fd: int, name: str) -> None:
            os.link(source, name, dst_dir_fd=destination_dir_fd)

        with patch(
            "zdecision.recall.demo.model_store._clone_file",
            side_effect=hardlink_alias,
        ):
            self._assert_error(
                "model_clone_unavailable",
                lambda: prepare_models(
                    profile=self.profile,
                    state_root=self.state_root,
                    snapshot_resolver=self._resolver,
                ),
            )

        self.assertEqual(source_mode, stat.S_IMODE(source.stat().st_mode))

    def test_cache_mutation_after_prepare_does_not_change_installed_bytes(self) -> None:
        """State bytes must remain valid when a later cache write changes its inode."""
        installed = self._prepare()
        state_path = installed.embedding_path / "config.json"
        before = state_path.read_bytes()
        source_path = self.snapshots[self.profile.embedding.model_id] / "config.json"

        source_path.write_bytes(b"cache changed after prepare\n")

        self.assertEqual(before, state_path.read_bytes())
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    def test_load_rejects_state_and_manifest_rewritten_away_from_signed_bytes(self) -> None:
        """Self-reported state hashes cannot override signed profile bindings."""
        installed = self._prepare()
        state_path = installed.embedding_path / "config.json"
        state_path.chmod(0o600)
        state_path.write_bytes(b"attacker-selected bytes\n")
        state_path.chmod(0o400)
        manifest_path = installed.install_manifest_path
        manifest_path.chmod(0o600)
        manifest = json.loads(manifest_path.read_text())
        record = next(
            item for item in manifest["files"] if item["path"] == "embedding/config.json"
        )
        record["size"] = state_path.stat().st_size
        record["sha256"] = hashlib.sha256(state_path.read_bytes()).hexdigest()
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        manifest_path.chmod(0o400)

        self._assert_error(
            "installed_manifest_invalid",
            lambda: load_installed_models(self.profile, self.state_root),
        )

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS clonefile")
    def test_macos_production_clone_has_distinct_inodes_and_source_isolation(self) -> None:
        """The real APFS clone path must not alias later source mutations."""
        installed = prepare_models(
            profile=self.profile,
            state_root=self.state_root,
            snapshot_resolver=self._resolver,
        )
        source = self.snapshots[self.profile.embedding.model_id] / "config.json"
        destination = installed.embedding_path / "config.json"
        self.assertNotEqual(
            (source.stat().st_dev, source.stat().st_ino),
            (destination.stat().st_dev, destination.stat().st_ino),
        )
        before = destination.read_bytes()

        source.write_bytes(b"changed source\n")

        self.assertEqual(before, destination.read_bytes())
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS extended ACLs")
    def test_macos_prepare_removes_inherited_acl_from_every_managed_path(self) -> None:
        """An inherited allow ACL must not survive as hidden non-owner access."""
        acl_parent = self.root / "acl-parent"
        acl_parent.mkdir(mode=0o700)
        _darwin_set_acl(acl_parent, _DARWIN_INHERITABLE_ACL)
        state_root = acl_parent / "state"

        installed = prepare_models(
            profile=self.profile,
            state_root=state_root,
            snapshot_resolver=self._resolver,
        )

        self.assertTrue(_darwin_has_acl(state_root))
        managed_root = state_root / "recall-demo"
        managed_paths = (managed_root, *sorted(managed_root.rglob("*")))
        self.assertTrue(managed_paths)
        self.assertEqual(
            [],
            [
                path.relative_to(managed_root).as_posix()
                for path in managed_paths
                if _darwin_has_acl(path)
            ],
        )
        self.assertEqual(installed, load_installed_models(self.profile, state_root))

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS extended ACLs")
    def test_macos_load_rejects_injected_acl_without_repairing_it(self) -> None:
        """Load must reject an ACL hidden behind an unchanged owner-only mode."""
        installed = self._prepare()
        model_path = installed.embedding_path / "config.json"
        mode_before = stat.S_IMODE(model_path.stat().st_mode)
        _darwin_set_acl(model_path, _DARWIN_FILE_ACL)

        self.assertEqual(0o400, mode_before)
        self.assertEqual(mode_before, stat.S_IMODE(model_path.stat().st_mode))
        self.assertTrue(_darwin_has_acl(model_path))
        self._assert_error(
            "installed_permissions_invalid",
            lambda: load_installed_models(self.profile, self.state_root),
        )
        self.assertTrue(_darwin_has_acl(model_path))

    def test_prepare_seals_exact_owner_and_modes(self) -> None:
        """Writable installed containers or files must not survive preparation."""
        installed = self._prepare()
        models_root = self.state_root / "recall-demo/models"
        containers = (
            self.state_root / "recall-demo",
            models_root,
            models_root / "staging",
            models_root / "installs",
        )
        sealed_directories = (
            installed.embedding_path.parent,
            installed.embedding_path,
            installed.reranker_path,
        )
        sealed_files = (
            *(installed.embedding_path / name for name in REQUIRED_FILES),
            *(installed.reranker_path / name for name in REQUIRED_FILES),
            installed.install_manifest_path,
            models_root / "current.json",
        )
        for path in containers:
            self.assertEqual(os.geteuid(), path.stat().st_uid)
            self.assertEqual(0o700, stat.S_IMODE(path.stat().st_mode))
        for path in sealed_directories:
            self.assertEqual(os.geteuid(), path.stat().st_uid)
            self.assertEqual(0o500, stat.S_IMODE(path.stat().st_mode))
        for path in sealed_files:
            self.assertEqual(os.geteuid(), path.stat().st_uid)
            self.assertEqual(0o400, stat.S_IMODE(path.stat().st_mode))

    def test_load_rejects_mode_or_owner_identity_mismatch(self) -> None:
        """Load must recheck both exact permission bits and the current owner."""
        installed = self._prepare()
        changed = installed.embedding_path / "config.json"
        changed.chmod(0o600)
        self._assert_error(
            "installed_permissions_invalid",
            lambda: load_installed_models(self.profile, self.state_root),
        )
        changed.chmod(0o400)

        with patch(
            "zdecision.recall.demo.model_store.os.geteuid",
            return_value=os.geteuid() + 1,
        ):
            self._assert_error(
                "state_root_invalid",
                lambda: load_installed_models(self.profile, self.state_root),
            )

    def test_load_rejects_schema_one_hardlink_install(self) -> None:
        """An old mutable hardlink generation requires explicit re-preparation."""
        installed = self._prepare()
        role_path = installed.embedding_path
        destination = role_path / "config.json"
        role_path.chmod(0o700)
        destination.unlink()
        os.link(
            self.snapshots[self.profile.embedding.model_id] / "config.json",
            destination,
        )
        role_path.chmod(0o500)
        manifest_path = installed.install_manifest_path
        manifest_path.chmod(0o600)
        manifest = json.loads(manifest_path.read_text())
        manifest["schema_version"] = 1
        manifest.pop("materialization")
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        manifest_path.chmod(0o400)

        self._assert_error(
            "installed_manifest_invalid",
            lambda: load_installed_models(self.profile, self.state_root),
        )

    def test_load_rejects_changed_or_deleted_installed_file(self) -> None:
        """A byte mutation or deletion must fail validation before model loading."""
        installed = self._prepare()
        changed = installed.embedding_path / "config.json"
        changed.chmod(0o600)
        changed.write_bytes(b"changed\n")
        changed.chmod(0o400)
        self._assert_error(
            "installed_file_digest_invalid",
            lambda: load_installed_models(self.profile, self.state_root),
        )

        with tempfile.TemporaryDirectory() as state_name:
            other_state = Path(state_name)
            installed = _prepare_tiny(
                profile=self.profile,
                state_root=other_state,
                snapshot_resolver=self._resolver,
            )
            installed.reranker_path.chmod(0o700)
            installed.reranker_path.joinpath("tokenizer.json").unlink()
            installed.reranker_path.chmod(0o500)
            self._assert_error(
                "installed_file_set_invalid",
                lambda: load_installed_models(self.profile, other_state),
            )

    def test_load_rejects_unexpected_profile_digest(self) -> None:
        """A pointer for another retrieval profile must never be accepted."""
        self._prepare()
        pointer_path = self.state_root / "recall-demo/models/current.json"
        pointer = json.loads(pointer_path.read_text())
        pointer["profile_digest"] = "0" * 64
        pointer_path.chmod(0o600)
        pointer_path.write_bytes(canonical_json_bytes(pointer))
        pointer_path.chmod(0o400)

        self._assert_error(
            "profile_digest_invalid",
            lambda: load_installed_models(self.profile, self.state_root),
        )

    def test_load_rejects_boolean_or_float_pointer_schema_version(self) -> None:
        """JSON true and 1.0 must not impersonate integer pointer schema 1."""
        for invalid_version in (True, 1.0):
            with self.subTest(invalid_version=invalid_version):
                with tempfile.TemporaryDirectory() as state_name:
                    state_root = Path(state_name)
                    _prepare_tiny(
                        profile=self.profile,
                        state_root=state_root,
                        snapshot_resolver=self._resolver,
                    )
                    pointer_path = state_root / "recall-demo/models/current.json"
                    pointer = json.loads(pointer_path.read_text())
                    pointer["schema_version"] = invalid_version
                    pointer_path.chmod(0o600)
                    pointer_path.write_bytes(canonical_json_bytes(pointer))
                    pointer_path.chmod(0o400)

                    self._assert_error(
                        "installed_pointer_invalid",
                        lambda: load_installed_models(self.profile, state_root),
                    )

    def test_load_rejects_boolean_or_float_manifest_schema_version(self) -> None:
        """JSON true and 1.0 must not impersonate integer manifest schema 2."""
        for invalid_version in (True, 1.0):
            with self.subTest(invalid_version=invalid_version):
                with tempfile.TemporaryDirectory() as state_name:
                    state_root = Path(state_name)
                    installed = _prepare_tiny(
                        profile=self.profile,
                        state_root=state_root,
                        snapshot_resolver=self._resolver,
                    )
                    manifest = json.loads(installed.install_manifest_path.read_text())
                    manifest["schema_version"] = invalid_version
                    installed.install_manifest_path.chmod(0o600)
                    installed.install_manifest_path.write_bytes(
                        canonical_json_bytes(manifest)
                    )
                    installed.install_manifest_path.chmod(0o400)

                    self._assert_error(
                        "installed_manifest_invalid",
                        lambda: load_installed_models(self.profile, state_root),
                    )

    def test_load_rejects_ancestor_symlinks_escaping_state_root(self) -> None:
        """A valid install redirected through an ancestor must not be accepted."""
        for redirected_name in ("recall-demo", "recall-demo/models"):
            with self.subTest(redirected_name=redirected_name):
                case_root = self.root / f"load-{redirected_name.replace('/', '-')}"
                actual_state = case_root / "actual-state"
                _prepare_tiny(
                    profile=self.profile,
                    state_root=actual_state,
                    snapshot_resolver=self._resolver,
                )
                alias_state = case_root / "alias-state"
                alias_state.mkdir(parents=True)
                redirected_path = alias_state / redirected_name
                redirected_path.parent.mkdir(parents=True, exist_ok=True)
                redirected_path.symlink_to(
                    actual_state / redirected_name, target_is_directory=True
                )

                self._assert_error(
                    "state_root_invalid",
                    lambda: load_installed_models(self.profile, alias_state),
                )

    def test_load_rejects_extra_duplicate_symlink_and_special_entries(self) -> None:
        """Unexpected or non-regular state entries must not escape validation."""

        def duplicate_manifest(installed: InstalledModels) -> None:
            manifest = json.loads(installed.install_manifest_path.read_text())
            manifest["files"].append(manifest["files"][0])
            installed.install_manifest_path.chmod(0o600)
            installed.install_manifest_path.write_bytes(canonical_json_bytes(manifest))
            installed.install_manifest_path.chmod(0o400)

        def replace_with_symlink(installed: InstalledModels) -> None:
            path = installed.embedding_path / "config.json"
            installed.embedding_path.chmod(0o700)
            path.unlink()
            path.symlink_to(
                self.snapshots[self.profile.embedding.model_id] / "config.json"
            )
            installed.embedding_path.chmod(0o500)

        def replace_with_fifo(installed: InstalledModels) -> None:
            path = installed.reranker_path / "special_tokens_map.json"
            installed.reranker_path.chmod(0o700)
            path.unlink()
            os.mkfifo(path)
            installed.reranker_path.chmod(0o500)

        def add_extra(installed: InstalledModels) -> None:
            installed.embedding_path.chmod(0o700)
            installed.embedding_path.joinpath("extra").write_bytes(b"x")
            installed.embedding_path.chmod(0o500)

        mutations = (
            add_extra,
            duplicate_manifest,
            replace_with_symlink,
            replace_with_fifo,
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as state_name:
                    state_root = Path(state_name)
                    installed = _prepare_tiny(
                        profile=self.profile,
                        state_root=state_root,
                        snapshot_resolver=self._resolver,
                    )
                    mutation(installed)
                    self._assert_error(
                        "installed_file_set_invalid",
                        lambda: load_installed_models(self.profile, state_root),
                    )

    def test_interrupted_second_prepare_preserves_previous_valid_manifest(self) -> None:
        """A failed later generation must not replace the active pointer or manifest."""
        installed = self._prepare()
        pointer_path = self.state_root / "recall-demo/models/current.json"
        pointer_before = pointer_path.read_bytes()
        manifest_before = installed.install_manifest_path.read_bytes()

        def interrupted_resolver(model_id: str, revision: str) -> Path:
            if model_id == self.profile.reranker.model_id:
                raise RuntimeError("simulated interruption")
            return self.snapshots[model_id]

        with self.assertRaises(RuntimeError):
            _prepare_tiny(
                profile=self.profile,
                state_root=self.state_root,
                snapshot_resolver=interrupted_resolver,
            )

        self.assertEqual(pointer_before, pointer_path.read_bytes())
        self.assertEqual(manifest_before, installed.install_manifest_path.read_bytes())
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    def test_pointer_replace_commits_a_presealed_canonical_sibling(self) -> None:
        """The sole pointer commit must rename an already durable 0400 document."""
        self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        pointer_path = models_root / "current.json"
        real_replace = os.replace
        real_fsync = os.fsync
        committed_sources: list[Path] = []
        committed_source_inodes: list[int] = []
        pointer_events: list[tuple[str, int]] = []

        def record_pointer_fsync(descriptor: int) -> None:
            status = os.fstat(descriptor)
            if stat.S_ISREG(status.st_mode) and stat.S_IMODE(status.st_mode) == 0o400:
                content = os.pread(descriptor, 4096, 0)
                if b'"install":' in content:
                    pointer_events.append(("fsync", status.st_ino))
            real_fsync(descriptor)

        def inspect_pointer_commit(source, destination) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if destination_path == pointer_path:
                committed_sources.append(source_path)
                committed_source_inodes.append(source_path.stat().st_ino)
                self.assertEqual(models_root, source_path.parent)
                self.assertNotEqual(pointer_path, source_path)
                self.assertEqual(os.geteuid(), source_path.stat().st_uid)
                self.assertEqual(0o400, stat.S_IMODE(source_path.stat().st_mode))
                content = source_path.read_bytes()
                self.assertEqual(
                    canonical_json_bytes(json.loads(content)),
                    content,
                )
                if sys.platform == "darwin":
                    self.assertFalse(_darwin_has_acl(source_path))
                pointer_events.append(("replace", source_path.stat().st_ino))
            real_replace(source, destination)

        with (
            patch(
                "zdecision.recall.demo.model_store.os.fsync",
                side_effect=record_pointer_fsync,
            ),
            patch(
                "zdecision.recall.demo.model_store.os.replace",
                side_effect=inspect_pointer_commit,
            ),
        ):
            self._prepare()

        self.assertEqual(1, len(committed_sources))
        self.assertEqual(
            [
                ("fsync", committed_source_inodes[0]),
                ("replace", committed_source_inodes[0]),
            ],
            pointer_events,
        )
        self.assertEqual(
            {"current.json", "installs", "staging"},
            {path.name for path in models_root.iterdir()},
        )

    def test_pointer_mode_failure_preserves_old_activation_and_cleans_new_state(self) -> None:
        """A precommit chmod failure must retain only the previous activation."""
        installed = self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        pointer_path = models_root / "current.json"
        pointer_before = pointer_path.read_bytes()
        pointer_inode_before = pointer_path.stat().st_ino
        installs_before = {path.name for path in (models_root / "installs").iterdir()}
        real_fchmod = os.fchmod

        def fail_pointer_mode(descriptor: int, mode: int) -> None:
            if mode == 0o400:
                content = os.pread(descriptor, 4096, 0)
                if b'"install":' in content:
                    raise OSError(errno.EIO, "injected pointer chmod failure")
            real_fchmod(descriptor, mode)

        with patch(
            "zdecision.recall.demo.model_store.os.fchmod",
            side_effect=fail_pointer_mode,
        ):
            self._assert_error("installed_pointer_invalid", self._prepare)

        self.assertEqual(pointer_before, pointer_path.read_bytes())
        self.assertEqual(pointer_inode_before, pointer_path.stat().st_ino)
        self.assertEqual(
            installs_before,
            {path.name for path in (models_root / "installs").iterdir()},
        )
        self.assertEqual(
            {"current.json", "installs", "staging"},
            {path.name for path in models_root.iterdir()},
        )
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    def test_generation_seal_failure_preserves_old_activation_and_cleans_staging(self) -> None:
        """A failure immediately after mkdir must not escape transaction cleanup."""
        installed = self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        staging_root = models_root / "staging"
        pointer_path = models_root / "current.json"
        pointer_before = pointer_path.read_bytes()
        pointer_inode_before = pointer_path.stat().st_ino
        installs_before = {path.name for path in (models_root / "installs").iterdir()}

        def fail_generation_seal(path: Path, mode: int, code: str) -> None:
            if path.parent == staging_root and len(path.name) == 32:
                raise ModelStoreError("state_root_invalid")
            _seal_directory(path, mode, code)

        with patch(
            "zdecision.recall.demo.model_store._seal_directory",
            side_effect=fail_generation_seal,
        ):
            self._assert_error("state_root_invalid", self._prepare)

        self.assertEqual(pointer_before, pointer_path.read_bytes())
        self.assertEqual(pointer_inode_before, pointer_path.stat().st_ino)
        self.assertEqual((), tuple(staging_root.iterdir()))
        self.assertEqual(
            installs_before,
            {path.name for path in (models_root / "installs").iterdir()},
        )
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    def test_install_rename_interrupt_cleans_new_install_and_preserves_activation(
        self,
    ) -> None:
        """An interrupt after the real install rename must not orphan its tree."""
        installed = self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        installs_root = models_root / "installs"
        staging_root = models_root / "staging"
        pointer_path = models_root / "current.json"
        pointer_before = pointer_path.read_bytes()
        pointer_inode_before = pointer_path.stat().st_ino
        installs_before = {path.name for path in installs_root.iterdir()}
        real_rename = os.rename

        def interrupt_after_install_rename(source, destination) -> None:
            real_rename(source, destination)
            if Path(destination).parent == installs_root:
                raise KeyboardInterrupt

        with patch(
            "zdecision.recall.demo.model_store.os.rename",
            side_effect=interrupt_after_install_rename,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._prepare()

        self.assertEqual(pointer_before, pointer_path.read_bytes())
        self.assertEqual(pointer_inode_before, pointer_path.stat().st_ino)
        self.assertEqual(
            installs_before,
            {path.name for path in installs_root.iterdir()},
        )
        self.assertEqual((), tuple(staging_root.iterdir()))
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    def test_install_rename_interrupt_before_mutation_is_missing_target_safe(
        self,
    ) -> None:
        """A pre-rename interrupt must not turn missing cleanup into a new error."""
        installed = self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        installs_root = models_root / "installs"
        staging_root = models_root / "staging"
        pointer_path = models_root / "current.json"
        pointer_before = pointer_path.read_bytes()
        pointer_inode_before = pointer_path.stat().st_ino
        installs_before = {path.name for path in installs_root.iterdir()}

        with patch(
            "zdecision.recall.demo.model_store.os.rename",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._prepare()

        self.assertEqual(pointer_before, pointer_path.read_bytes())
        self.assertEqual(pointer_inode_before, pointer_path.stat().st_ino)
        self.assertEqual(
            installs_before,
            {path.name for path in installs_root.iterdir()},
        )
        self.assertEqual((), tuple(staging_root.iterdir()))
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    def test_install_rename_race_does_not_remove_untracked_destination(self) -> None:
        """Cleanup must not delete a same-name directory it did not rename."""
        installed = self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        staging_root = models_root / "staging"
        pointer_path = models_root / "current.json"
        pointer_before = pointer_path.read_bytes()
        foreign_markers: list[Path] = []

        def occupy_install_destination(_source, destination) -> None:
            destination_path = Path(destination)
            destination_path.mkdir(mode=0o700)
            marker = destination_path / "foreign"
            marker.write_bytes(b"not-this-transaction")
            foreign_markers.append(marker)
            raise KeyboardInterrupt

        with patch(
            "zdecision.recall.demo.model_store.os.rename",
            side_effect=occupy_install_destination,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._prepare()

        self.assertEqual(1, len(foreign_markers))
        self.assertTrue(foreign_markers[0].exists())
        self.assertEqual(b"not-this-transaction", foreign_markers[0].read_bytes())
        self.assertEqual(pointer_before, pointer_path.read_bytes())
        self.assertEqual((), tuple(staging_root.iterdir()))
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    def test_pointer_replace_failure_preserves_old_activation_and_cleans_new_state(self) -> None:
        """A failed commit rename must remove its candidate and unreferenced install."""
        installed = self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        pointer_path = models_root / "current.json"
        pointer_before = pointer_path.read_bytes()
        pointer_inode_before = pointer_path.stat().st_ino
        installs_before = {path.name for path in (models_root / "installs").iterdir()}
        real_replace = os.replace

        def fail_pointer_commit(source, destination) -> None:
            if Path(destination) == pointer_path:
                raise OSError(errno.EIO, "injected pointer replace failure")
            real_replace(source, destination)

        with patch(
            "zdecision.recall.demo.model_store.os.replace",
            side_effect=fail_pointer_commit,
        ):
            self._assert_error("installed_pointer_invalid", self._prepare)

        self.assertEqual(pointer_before, pointer_path.read_bytes())
        self.assertEqual(pointer_inode_before, pointer_path.stat().st_ino)
        self.assertEqual(
            installs_before,
            {path.name for path in (models_root / "installs").iterdir()},
        )
        self.assertEqual(
            {"current.json", "installs", "staging"},
            {path.name for path in models_root.iterdir()},
        )
        self.assertEqual(installed, load_installed_models(self.profile, self.state_root))

    def test_pointer_replace_interrupt_retains_committed_install(self) -> None:
        """An interrupt after the real pointer replace must retain its target."""
        self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        installs_root = models_root / "installs"
        staging_root = models_root / "staging"
        pointer_path = models_root / "current.json"
        pointer_inode_before = pointer_path.stat().st_ino
        installs_before = {path.name for path in installs_root.iterdir()}
        real_replace = os.replace

        def interrupt_after_pointer_replace(source, destination) -> None:
            real_replace(source, destination)
            if Path(destination) == pointer_path:
                raise KeyboardInterrupt

        with patch(
            "zdecision.recall.demo.model_store.os.replace",
            side_effect=interrupt_after_pointer_replace,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._prepare()

        self.assertNotEqual(pointer_inode_before, pointer_path.stat().st_ino)
        pointer = json.loads(pointer_path.read_text())
        referenced_install = models_root / pointer["install"]
        self.assertTrue(referenced_install.is_dir())
        self.assertEqual(
            len(installs_before) + 1,
            len(tuple(installs_root.iterdir())),
        )
        self.assertEqual((), tuple(staging_root.iterdir()))
        self.assertEqual(
            {"current.json", "installs", "staging"},
            {path.name for path in models_root.iterdir()},
        )
        self.assertEqual(
            referenced_install,
            load_installed_models(self.profile, self.state_root).embedding_path.parent,
        )

    def test_postcommit_sync_failure_retains_the_referenced_install(self) -> None:
        """Once replace succeeds, later failure must not delete its target install."""
        self._prepare()
        models_root = (self.state_root / "recall-demo/models").resolve()
        pointer_path = models_root / "current.json"
        pointer_inode_before = pointer_path.stat().st_ino
        installs_before = {path.name for path in (models_root / "installs").iterdir()}
        real_sync_directory = _fsync_directory

        def fail_models_sync(path: Path) -> None:
            if path == models_root:
                raise OSError(errno.EIO, "injected postcommit sync failure")
            real_sync_directory(path)

        with patch(
            "zdecision.recall.demo.model_store._fsync_directory",
            side_effect=fail_models_sync,
        ):
            with self.assertRaises(OSError):
                self._prepare()

        self.assertNotEqual(pointer_inode_before, pointer_path.stat().st_ino)
        pointer = json.loads(pointer_path.read_text())
        referenced_install = models_root / pointer["install"]
        self.assertTrue(referenced_install.is_dir())
        self.assertEqual(0o400, stat.S_IMODE(pointer_path.stat().st_mode))
        self.assertEqual(
            len(installs_before) + 1,
            len(tuple((models_root / "installs").iterdir())),
        )
        self.assertEqual(
            {"current.json", "installs", "staging"},
            {path.name for path in models_root.iterdir()},
        )
        self.assertEqual(
            referenced_install,
            load_installed_models(self.profile, self.state_root).embedding_path.parent,
        )


class _DeterministicEmbedding:
    dimension = 384

    def embed_query(self, text: str) -> tuple[float, ...]:
        return (1.0,) + (0.0,) * 383

    def embed_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        return tuple(
            (float(index),) + (0.0,) * 382 + (1.0,)
            for index, _text in enumerate(texts, start=1)
        )


class _DeterministicReranker:
    def score(self, query: str, documents: tuple[str, ...]) -> tuple[float, ...]:
        return tuple(float(len(document)) for document in documents)


class RuntimeProtocolTests(unittest.TestCase):
    """Runtime-neutral fakes demonstrate the exact caller-facing shapes and order."""

    def test_fake_bundle_exposes_normalized_query_and_ordered_batches(self) -> None:
        """A dimension drift or reordered document/pair batch must fail callers."""
        bundle = ModelRuntimeBundle(
            profile_digest="a" * 64,
            embedding=_DeterministicEmbedding(),
            reranker=_DeterministicReranker(),
        )

        query = bundle.embedding.embed_query("中文 and English")
        documents = bundle.embedding.embed_documents(("first", "second"))
        scores = bundle.reranker.score("query", ("longer", "x"))

        self.assertEqual(384, bundle.embedding.dimension)
        self.assertEqual(384, len(query))
        self.assertTrue(all(math.isfinite(value) for value in query))
        self.assertAlmostEqual(1.0, math.sqrt(sum(value * value for value in query)))
        self.assertEqual((1.0, 2.0), (documents[0][0], documents[1][0]))
        self.assertEqual((6.0, 1.0), scores)


class _EmbeddingTokenizer:
    def __init__(self) -> None:
        self.inputs: list[tuple[str, ...]] = []
        self.options: list[dict[str, object]] = []

    def __call__(self, texts, **kwargs):
        values = tuple(texts)
        self.inputs.append(values)
        self.options.append(kwargs)
        identifiers = []
        for text in values:
            if text.startswith("query: "):
                identifier = 1
            elif text == "passage: first":
                identifier = 2
            elif text == "passage: second":
                identifier = 3
            else:
                identifier = 4
            identifiers.append([identifier, identifier])
        return {
            "input_ids": torch.tensor(identifiers, dtype=torch.long),
            "attention_mask": torch.ones((len(identifiers), 2), dtype=torch.long),
        }


class _RerankerTokenizer:
    def __init__(self) -> None:
        self.pairs: list[tuple[tuple[str, str], ...]] = []
        self.options: list[dict[str, object]] = []

    def __call__(self, queries, documents, **kwargs):
        self.pairs.append(tuple(zip(queries, documents, strict=True)))
        self.options.append(kwargs)
        values = {"first": 1.0, "second": 2.0}
        return {
            "input_ids": torch.tensor(
                [[values.get(document, 3.0)] for document in documents],
                dtype=torch.float32,
            ),
            "attention_mask": torch.ones((len(documents), 1), dtype=torch.long),
        }


class _EmbeddingModel:
    def to(self, device):
        return self

    def eval(self):
        return self

    def __call__(self, *, input_ids, attention_mask):
        batch = []
        for identifier in input_ids[:, 0].tolist():
            vector = torch.zeros(384, dtype=torch.float32)
            vector[0] = float(identifier)
            vector[1] = 1.0
            batch.append(torch.stack((vector, vector)))
        return SimpleNamespace(last_hidden_state=torch.stack(batch))


class _RerankerModel:
    def __init__(self) -> None:
        self.logits_override = None

    def to(self, device):
        return self

    def eval(self):
        return self

    def __call__(self, *, input_ids, attention_mask):
        logits = self.logits_override
        if logits is None:
            logits = input_ids[:, :1]
        return SimpleNamespace(logits=logits)


class TransformersRuntimeTests(unittest.TestCase):
    """Production adapters load verified local paths and validate model outputs."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        committed_profile = _profile()
        self.snapshots = {
            committed_profile.embedding.model_id: _make_snapshot(
                self.root / "cache", "embedding"
            ),
            committed_profile.reranker.model_id: _make_snapshot(
                self.root / "cache", "reranker"
            ),
        }
        self.profile = _profile_for_snapshots(self.snapshots)
        self.installed = _prepare_tiny(
            profile=self.profile,
            state_root=self.state_root,
            snapshot_resolver=lambda model_id, revision: self.snapshots[model_id],
        )
        self.embedding_tokenizer = _EmbeddingTokenizer()
        self.reranker_tokenizer = _RerankerTokenizer()
        self.embedding_model = _EmbeddingModel()
        self.reranker_model = _RerankerModel()
        self.loader_calls: list[tuple[str, str, dict[str, object]]] = []

    def tearDown(self) -> None:
        _make_tree_writable(self.root)
        self.temporary.cleanup()

    def _load_bundle(self) -> ModelRuntimeBundle:
        embedding_path = str(self.installed.embedding_path)
        reranker_path = str(self.installed.reranker_path)

        def tokenizer_loader(path: str, **kwargs):
            self.loader_calls.append(("tokenizer", path, kwargs))
            if path == embedding_path:
                return self.embedding_tokenizer
            if path == reranker_path:
                return self.reranker_tokenizer
            raise AssertionError(f"unexpected tokenizer path: {path}")

        def embedding_loader(path: str, **kwargs):
            self.loader_calls.append(("embedding", path, kwargs))
            if path != embedding_path:
                raise AssertionError(f"unexpected embedding path: {path}")
            return self.embedding_model

        def reranker_loader(path: str, **kwargs):
            self.loader_calls.append(("reranker", path, kwargs))
            if path != reranker_path:
                raise AssertionError(f"unexpected reranker path: {path}")
            return self.reranker_model

        with (
            patch("transformers.AutoTokenizer.from_pretrained", side_effect=tokenizer_loader),
            patch("transformers.AutoModel.from_pretrained", side_effect=embedding_loader),
            patch(
                "transformers.AutoModelForSequenceClassification.from_pretrained",
                side_effect=reranker_loader,
            ),
        ):
            return load_transformers_runtime(
                self.profile, self.installed, device="cpu"
            )

    def _load_with(
        self,
        *,
        tokenizer_loader=None,
        embedding_loader=None,
        reranker_loader=None,
        device: str | None = "cpu",
    ) -> ModelRuntimeBundle:
        embedding_path = str(self.installed.embedding_path)
        reranker_path = str(self.installed.reranker_path)

        if tokenizer_loader is None:
            def tokenizer_loader(path: str, **kwargs):
                if path == embedding_path:
                    return self.embedding_tokenizer
                if path == reranker_path:
                    return self.reranker_tokenizer
                raise AssertionError(f"unexpected tokenizer path: {path}")
        if embedding_loader is None:
            embedding_loader = lambda path, **kwargs: self.embedding_model
        if reranker_loader is None:
            reranker_loader = lambda path, **kwargs: self.reranker_model

        with (
            patch(
                "transformers.AutoTokenizer.from_pretrained",
                side_effect=tokenizer_loader,
            ),
            patch(
                "transformers.AutoModel.from_pretrained",
                side_effect=embedding_loader,
            ),
            patch(
                "transformers.AutoModelForSequenceClassification.from_pretrained",
                side_effect=reranker_loader,
            ),
        ):
            return load_transformers_runtime(
                self.profile, self.installed, device=device
            )

    def _assert_runtime_load_failed(self, action) -> None:
        with self.assertRaises(DemoRuntimeError) as captured:
            action()
        self.assertEqual("runtime_load_failed", captured.exception.code)
        self.assertEqual("runtime_load_failed", str(captured.exception))
        self.assertNotIn(str(self.root), str(captured.exception))

    def test_loader_uses_only_absolute_verified_local_snapshots(self) -> None:
        """A network-enabled, remote-code, or unverified loader argument must fail."""
        bundle = self._load_bundle()

        self.assertEqual(self.profile.digest, bundle.profile_digest)
        kwargs = {"local_files_only": True, "trust_remote_code": False}
        self.assertEqual(
            [
                ("tokenizer", str(self.installed.embedding_path), kwargs),
                ("embedding", str(self.installed.embedding_path), kwargs),
                ("tokenizer", str(self.installed.reranker_path), kwargs),
                ("reranker", str(self.installed.reranker_path), kwargs),
            ],
            self.loader_calls,
        )
        self.assertTrue(
            all(Path(path).is_absolute() for _kind, path, _kwargs in self.loader_calls)
        )

    def test_loader_revalidates_post_install_mutation_before_transformers(self) -> None:
        """Changed state bytes must fail before the first Transformers reopen."""
        path = self.installed.embedding_path / "config.json"
        path.chmod(0o600)
        path.write_bytes(b"changed after installation\n")
        path.chmod(0o400)
        calls: list[str] = []

        with patch(
            "transformers.AutoTokenizer.from_pretrained",
            side_effect=lambda *args, **kwargs: calls.append("transformers"),
        ):
            with self.assertRaises(DemoRuntimeError) as captured:
                load_transformers_runtime(
                    self.profile, self.installed, device="cpu"
                )

        self.assertEqual("installed_models_invalid", captured.exception.code)
        self.assertEqual([], calls)

    def test_loader_sanitizes_third_party_import_and_device_failures(self) -> None:
        """Import or accelerator probing failures must expose only a domain code."""
        with self.subTest(boundary="torch-import"):
            with patch.dict(sys.modules, {"torch": None}):
                self._assert_runtime_load_failed(
                    lambda: load_transformers_runtime(
                        self.profile, self.installed, device="cpu"
                    )
                )

        with self.subTest(boundary="transformers-import"):
            with patch.dict(sys.modules, {"transformers": None}):
                self._assert_runtime_load_failed(
                    lambda: load_transformers_runtime(
                        self.profile, self.installed, device="cpu"
                    )
                )

        secret = str(self.root / "mps-device-details")
        with self.subTest(boundary="mps-is-available"):
            with patch(
                "torch.backends.mps.is_available",
                side_effect=RuntimeError(secret),
            ):
                self._assert_runtime_load_failed(
                    lambda: load_transformers_runtime(
                        self.profile, self.installed, device=None
                    )
                )

    def test_loader_sanitizes_each_from_pretrained_failure(self) -> None:
        """Each local Transformers loader must convert expected load failures."""
        embedding_path = str(self.installed.embedding_path)
        secret = str(self.root / "third-party-loader-details")

        def fail_embedding_tokenizer(path: str, **kwargs):
            raise OSError(secret)

        def fail_embedding_model(path: str, **kwargs):
            raise ValueError(secret)

        def fail_reranker_tokenizer(path: str, **kwargs):
            if path == embedding_path:
                return self.embedding_tokenizer
            raise RuntimeError(secret)

        def fail_reranker_model(path: str, **kwargs):
            raise OSError(secret)

        cases = (
            (
                "embedding-tokenizer",
                lambda: self._load_with(
                    tokenizer_loader=fail_embedding_tokenizer
                ),
            ),
            (
                "embedding-model",
                lambda: self._load_with(
                    embedding_loader=fail_embedding_model
                ),
            ),
            (
                "reranker-tokenizer",
                lambda: self._load_with(
                    tokenizer_loader=fail_reranker_tokenizer
                ),
            ),
            (
                "reranker-model",
                lambda: self._load_with(reranker_loader=fail_reranker_model),
            ),
        )
        for boundary, action in cases:
            with self.subTest(boundary=boundary):
                self._assert_runtime_load_failed(action)

    def test_loader_sanitizes_safetensor_failure_without_path_leakage(self) -> None:
        """A SafetensorError from local model loading must not expose its path."""
        secret = str(self.root / "private-cache/model.safetensors")

        def fail_safetensor_load(path: str, **kwargs):
            raise SafetensorError(secret)

        with self.assertRaises(Exception) as captured:
            self._load_with(embedding_loader=fail_safetensor_load)

        self.assertIs(DemoRuntimeError, type(captured.exception))
        self.assertEqual("runtime_load_failed", captured.exception.code)
        self.assertEqual("runtime_load_failed", str(captured.exception))
        self.assertNotIn(secret, str(captured.exception))

    def test_cli_runtime_wrapper_propagates_custom_development_error(self) -> None:
        """A custom error from the whole runtime loader seam must remain visible."""
        from zdecision.recall.demo import cli

        class RuntimeDevelopmentError(Exception):
            pass

        expected = RuntimeDevelopmentError("visible-custom-development-error")
        with patch.object(
            cli,
            "load_transformers_runtime",
            side_effect=expected,
        ):
            with self.assertRaises(RuntimeDevelopmentError) as captured:
                cli.CliDependencies().load_runtime(self.profile, self.installed)

        self.assertIs(expected, captured.exception)

    def test_loader_sanitizes_each_model_placement_failure(self) -> None:
        """Both model placement/eval chains must convert expected load failures."""
        secret = str(self.root / "third-party-placement-details")
        with self.subTest(boundary="embedding-to"):
            with patch.object(
                self.embedding_model,
                "to",
                side_effect=RuntimeError(secret),
            ):
                self._assert_runtime_load_failed(self._load_with)

        with self.subTest(boundary="embedding-eval"):
            with patch.object(
                self.embedding_model,
                "eval",
                side_effect=OSError(secret),
            ):
                self._assert_runtime_load_failed(self._load_with)

        with self.subTest(boundary="reranker-to"):
            with patch.object(
                self.reranker_model,
                "to",
                side_effect=RuntimeError(secret),
            ):
                self._assert_runtime_load_failed(self._load_with)

        with self.subTest(boundary="reranker-eval"):
            with patch.object(
                self.reranker_model,
                "eval",
                side_effect=ValueError(secret),
            ):
                self._assert_runtime_load_failed(self._load_with)

    def test_loader_preserves_existing_domain_errors(self) -> None:
        """A domain failure at a guarded boundary must not be rewritten."""
        expected = DemoRuntimeError("installed_models_invalid")

        def fail_with_domain_error(path: str, **kwargs):
            raise expected

        with self.assertRaises(DemoRuntimeError) as captured:
            self._load_with(tokenizer_loader=fail_with_domain_error)
        self.assertIs(expected, captured.exception)

    def test_e5_prefixes_normalizes_and_preserves_document_order(self) -> None:
        """Missing E5 prefixes, pooling, normalization, or batch order must fail."""
        bundle = self._load_bundle()

        query = bundle.embedding.embed_query("中英 mixed query")
        documents = bundle.embedding.embed_documents(("first", "second"))

        self.assertEqual(384, len(query))
        self.assertTrue(all(math.isfinite(value) for value in query))
        self.assertAlmostEqual(1.0, math.sqrt(sum(value * value for value in query)))
        self.assertAlmostEqual(2.0 / math.sqrt(5.0), documents[0][0])
        self.assertAlmostEqual(3.0 / math.sqrt(10.0), documents[1][0])
        self.assertEqual(
            [("query: 中英 mixed query",), ("passage: first", "passage: second")],
            self.embedding_tokenizer.inputs,
        )
        self.assertEqual(
            [
                {
                    "padding": True,
                    "truncation": True,
                    "max_length": 512,
                    "return_tensors": "pt",
                },
                {
                    "padding": True,
                    "truncation": True,
                    "max_length": 512,
                    "return_tensors": "pt",
                },
            ],
            self.embedding_tokenizer.options,
        )

    def test_reranker_preserves_pair_order_and_rejects_invalid_logits(self) -> None:
        """Reordered, non-finite, or wrong-count logits must not reach retrieval."""
        bundle = self._load_bundle()

        self.assertEqual((2.0, 1.0), bundle.reranker.score("q", ("second", "first")))
        self.assertEqual(
            [(('q', 'second'), ('q', 'first'))], self.reranker_tokenizer.pairs
        )
        self.assertEqual(
            {
                "padding": True,
                "truncation": True,
                "max_length": 512,
                "return_tensors": "pt",
            },
            self.reranker_tokenizer.options[0],
        )
        for logits in (
            torch.tensor([[math.nan], [1.0]]),
            torch.tensor([[1.0]]),
        ):
            with self.subTest(logits=logits):
                self.reranker_model.logits_override = logits
                with self.assertRaises(DemoRuntimeError) as captured:
                    bundle.reranker.score("q", ("first", "second"))
                self.assertEqual("reranker_output_invalid", captured.exception.code)
                self.assertEqual(captured.exception.code, str(captured.exception))

    def test_loader_rejects_installed_paths_not_returned_by_model_store(self) -> None:
        """A caller-constructed relative model location must not reach Transformers."""
        unverified = InstalledModels(
            profile_digest=self.profile.digest,
            embedding_path=Path("embedding"),
            reranker_path=Path("reranker"),
            install_manifest_path=Path("model-install.json"),
        )

        with self.assertRaises(DemoRuntimeError) as captured:
            load_transformers_runtime(self.profile, unverified, device="cpu")
        self.assertEqual("installed_models_invalid", captured.exception.code)


@unittest.skipUnless(
    os.environ.get("ZDECISION_RUN_REAL_MODEL_SMOKE") == "1",
    "set ZDECISION_RUN_REAL_MODEL_SMOKE=1 to load prepared local models",
)
class RealRuntimeSmokeTests(unittest.TestCase):
    """Opt-in shape/finite smoke against already prepared pinned snapshots."""

    def test_prepared_models_embed_and_rerank_without_network(self) -> None:
        profile = _profile()
        state_root = Path(
            os.environ.get(
                "ZDECISION_RECALL_DEMO_STATE_DIR",
                "/Users/zhaohuiying/Library/Caches/zdecision-demo/recall-prototype/state",
            )
        )
        installed = load_installed_models(profile, state_root)
        bundle = load_transformers_runtime(profile, installed)

        query = bundle.embedding.embed_query("清理 permission check for cleanup")
        documents = bundle.embedding.embed_documents(
            ("Decision: cleanup requires explicit authorization.",)
        )
        scores = bundle.reranker.score(
            "cleanup permission", ("explicit authorization",)
        )

        self.assertEqual(384, len(query))
        self.assertEqual((384,), tuple(len(vector) for vector in documents))
        self.assertTrue(all(math.isfinite(value) for value in query))
        self.assertTrue(all(math.isfinite(value) for value in documents[0]))
        self.assertEqual(1, len(scores))
        self.assertTrue(math.isfinite(scores[0]))


if __name__ == "__main__":
    unittest.main()
