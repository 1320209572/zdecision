"""Signed, content-verified local model installation for the recall demo."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from zdecision.jsonio import atomic_write_json, canonical_json_bytes

from zdecision.recall.demo.contracts import DemoModelSpec, DemoRetrievalProfile

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on unsupported platforms
    fcntl = None  # type: ignore[assignment]


_ROLES = ("embedding", "reranker")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MATERIALIZATION = "cow-clone-v1"
_CLONE_NOOWNERCOPY = 0x0002
_ACL_TYPE_EXTENDED = 0x00000100
_GENERIC_FICLONE = 0x40049409
_ALT_DIRECTION_FICLONE = 0x80049409


@dataclass(frozen=True)
class InstalledModels:
    profile_digest: str
    embedding_path: Path
    reranker_path: Path
    install_manifest_path: Path


@dataclass(frozen=True)
class InstalledModelMetadata:
    """Prepared-model facts safe to validate before loading model tensors."""

    profile_digest: str
    install_manifest_digest: str


class ModelStoreError(RuntimeError):
    """A sanitized local model preparation or validation failure."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


SnapshotResolver = Callable[[str, str], Path]


def prepare_models(
    *,
    profile: DemoRetrievalProfile,
    state_root: Path,
    snapshot_resolver: SnapshotResolver,
) -> InstalledModels:
    """CoW-clone both signed snapshots into an atomically activated install."""

    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise ModelStoreError("model_clone_unavailable")
    root = _absolute_state_root(state_root)
    models_root, staging_root, installs_root = _prepare_state_directories(root)
    generation = staging_root / uuid.uuid4().hex
    pointer_path = models_root / "current.json"
    new_install_path: Path | None = None
    new_install_identity: tuple[int, int] | None = None
    pointer_candidate: Path | None = None
    pointer_candidate_identity: tuple[int, int] | None = None
    try:
        generation.mkdir(mode=0o700)
        _seal_directory(generation, 0o700, "state_root_invalid")
        for role, spec in (
            ("embedding", profile.embedding),
            ("reranker", profile.reranker),
        ):
            destination_root = generation / role
            destination_root.mkdir(mode=0o700)
            _seal_directory(
                destination_root, 0o700, "installed_permissions_invalid"
            )
            source_root = Path(snapshot_resolver(spec.model_id, spec.revision))
            _clone_snapshot(source_root, destination_root, spec)

        expected_files = _expected_file_records(profile)
        _validate_actual_files(
            generation, expected_files, sealed=False, include_manifest=False
        )
        manifest = {
            "schema_version": 2,
            "materialization": _MATERIALIZATION,
            "profile_digest": profile.digest,
            "models": _expected_models(profile),
            "files": expected_files,
        }
        manifest_path = generation / "model-install.json"
        atomic_write_json(manifest_path, manifest)
        _seal_regular_file(manifest_path, 0o400, "installed_manifest_invalid")
        _sync_tree(generation)
        _validate_install(generation, profile, expected_files, sealed=False)

        install_path = installs_root / profile.digest
        if install_path.exists() or install_path.is_symlink():
            install_path = installs_root / f"{profile.digest}-{uuid.uuid4().hex}"
        generation_status = generation.lstat()
        new_install_identity = (
            generation_status.st_dev,
            generation_status.st_ino,
        )
        new_install_path = install_path
        os.rename(generation, install_path)
        for role in _ROLES:
            _seal_directory(
                install_path / role, 0o500, "installed_permissions_invalid"
            )
        _seal_directory(install_path, 0o500, "installed_permissions_invalid")
        _validate_install(install_path, profile, expected_files, sealed=True)
        _fsync_directory(installs_root)
        pointer_candidate = _write_sealed_pointer_candidate(
            models_root,
            {
                "schema_version": 1,
                "profile_digest": profile.digest,
                "install": install_path.relative_to(models_root).as_posix(),
            },
        )
        candidate_status = pointer_candidate.lstat()
        pointer_candidate_identity = (
            candidate_status.st_dev,
            candidate_status.st_ino,
        )
        try:
            os.replace(pointer_candidate, pointer_path)
        except OSError:
            raise ModelStoreError("installed_pointer_invalid") from None
        _fsync_directory(models_root)
    except BaseException:
        pointer_committed = (
            pointer_candidate_identity is not None
            and _path_has_file_identity(pointer_path, pointer_candidate_identity)
        )
        if not pointer_committed:
            if pointer_candidate is not None:
                pointer_candidate.unlink(missing_ok=True)
            if new_install_path is not None and new_install_identity is not None:
                _remove_uncommitted_install(
                    new_install_path,
                    installs_root,
                    profile.digest,
                    new_install_identity,
                )
        raise
    finally:
        if generation.exists():
            _remove_staging_generation(generation, staging_root)

    return load_installed_models(profile, root)


def load_installed_models(
    profile: DemoRetrievalProfile, state_root: Path
) -> InstalledModels:
    """Require signed expectations, manifest records, and actual bytes to agree."""

    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise ModelStoreError("model_clone_unavailable")
    root = _absolute_state_root(state_root)
    models_root, installs_root = _load_state_directories(root)
    pointer_path = models_root / "current.json"
    _require_owned_mode(pointer_path, 0o400, "installed_pointer_invalid")
    pointer = _read_canonical_mapping(pointer_path, "installed_pointer_invalid")
    if frozenset(pointer) != frozenset(("schema_version", "profile_digest", "install")):
        raise ModelStoreError("installed_pointer_invalid")
    if not _is_schema_version(pointer["schema_version"], 1):
        raise ModelStoreError("installed_pointer_invalid")
    if pointer["profile_digest"] != profile.digest:
        raise ModelStoreError("profile_digest_invalid")

    install_value = pointer["install"]
    if not isinstance(install_value, str):
        raise ModelStoreError("installed_pointer_invalid")
    relative_install = Path(install_value)
    if (
        relative_install.is_absolute()
        or len(relative_install.parts) != 2
        or relative_install.parts[0] != "installs"
        or relative_install.parts[1] in ("", ".", "..")
    ):
        raise ModelStoreError("installed_pointer_invalid")
    install_path = models_root / relative_install
    try:
        resolved_install = install_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ModelStoreError("installed_pointer_invalid") from None
    if (
        install_path.is_symlink()
        or resolved_install.parent != installs_root
        or not resolved_install.is_dir()
    ):
        raise ModelStoreError("installed_pointer_invalid")

    expected_files = _expected_file_records(profile)
    _validate_install(resolved_install, profile, expected_files, sealed=True)
    return InstalledModels(
        profile_digest=profile.digest,
        embedding_path=resolved_install / "embedding",
        reranker_path=resolved_install / "reranker",
        install_manifest_path=resolved_install / "model-install.json",
    )


def load_installed_model_metadata(
    profile: DemoRetrievalProfile, state_root: Path
) -> InstalledModelMetadata:
    """Verify owner-only install identity without reading model tensor bytes."""
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise ModelStoreError("model_clone_unavailable")
    root = _absolute_state_root(state_root)
    models_root, installs_root = _load_state_directories(root)
    pointer_path = models_root / "current.json"
    _require_owned_mode(pointer_path, 0o400, "installed_pointer_invalid")
    pointer = _read_canonical_mapping(pointer_path, "installed_pointer_invalid")
    if frozenset(pointer) != frozenset(("schema_version", "profile_digest", "install")):
        raise ModelStoreError("installed_pointer_invalid")
    if not _is_schema_version(pointer["schema_version"], 1):
        raise ModelStoreError("installed_pointer_invalid")
    if pointer["profile_digest"] != profile.digest:
        raise ModelStoreError("profile_digest_invalid")
    install_value = pointer["install"]
    if not isinstance(install_value, str):
        raise ModelStoreError("installed_pointer_invalid")
    relative_install = Path(install_value)
    if (
        relative_install.is_absolute()
        or len(relative_install.parts) != 2
        or relative_install.parts[0] != "installs"
        or relative_install.parts[1] in ("", ".", "..")
    ):
        raise ModelStoreError("installed_pointer_invalid")
    install_path = models_root / relative_install
    try:
        resolved_install = install_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ModelStoreError("installed_pointer_invalid") from None
    if (
        install_path.is_symlink()
        or resolved_install.parent != installs_root
        or not resolved_install.is_dir()
    ):
        raise ModelStoreError("installed_pointer_invalid")
    _validate_install_metadata(resolved_install, profile)
    manifest_path = resolved_install / "model-install.json"
    return InstalledModelMetadata(
        profile_digest=profile.digest,
        install_manifest_digest=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )


def _absolute_state_root(state_root: Path) -> Path:
    root = Path(state_root)
    if not root.is_absolute():
        raise ModelStoreError("state_root_invalid")
    return root


def _is_schema_version(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _darwin_acl_api(code: str):
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        acl_init = libc.acl_init
        acl_init.argtypes = [ctypes.c_int]
        acl_init.restype = ctypes.c_void_p
        acl_set_fd_np = libc.acl_set_fd_np
        acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        acl_set_fd_np.restype = ctypes.c_int
        acl_get_fd_np = libc.acl_get_fd_np
        acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        acl_get_fd_np.restype = ctypes.c_void_p
        acl_free = libc.acl_free
        acl_free.argtypes = [ctypes.c_void_p]
        acl_free.restype = ctypes.c_int
    except (AttributeError, OSError):
        raise ModelStoreError(code) from None
    return acl_init, acl_set_fd_np, acl_get_fd_np, acl_free


def _require_no_extended_acl_fd(descriptor: int, code: str) -> None:
    if sys.platform != "darwin":
        return
    _acl_init, _acl_set_fd_np, acl_get_fd_np, acl_free = _darwin_acl_api(code)
    ctypes.set_errno(0)
    acl = acl_get_fd_np(descriptor, _ACL_TYPE_EXTENDED)
    error_number = ctypes.get_errno()
    if acl is not None:
        acl_free(acl)
        raise ModelStoreError(code)
    if error_number != errno.ENOENT:
        raise ModelStoreError(code)


def _clear_extended_acl_fd(descriptor: int, code: str) -> None:
    if sys.platform != "darwin":
        return
    acl_init, acl_set_fd_np, _acl_get_fd_np, acl_free = _darwin_acl_api(code)
    ctypes.set_errno(0)
    empty_acl = acl_init(0)
    if empty_acl is None:
        raise ModelStoreError(code)
    set_result = acl_set_fd_np(descriptor, empty_acl, _ACL_TYPE_EXTENDED)
    free_result = acl_free(empty_acl)
    if set_result != 0 or free_result != 0:
        raise ModelStoreError(code)
    _require_no_extended_acl_fd(descriptor, code)


def _open_directory(path: Path, code: str) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        status = os.fstat(descriptor)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise ModelStoreError(code) from None
    if not stat.S_ISDIR(status.st_mode):
        os.close(descriptor)
        raise ModelStoreError(code)
    return descriptor


def _seal_directory(path: Path, mode: int, code: str) -> None:
    descriptor = _open_directory(path, code)
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise ModelStoreError(code)
        _clear_extended_acl_fd(descriptor, code)
        os.fchmod(descriptor, mode)
        status = os.fstat(descriptor)
        _require_no_extended_acl_fd(descriptor, code)
        if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != mode:
            raise ModelStoreError(code)
        os.fsync(descriptor)
    except OSError:
        raise ModelStoreError(code) from None
    finally:
        os.close(descriptor)


def _require_directory(path: Path, mode: int, code: str) -> None:
    descriptor = _open_directory(path, code)
    try:
        status = os.fstat(descriptor)
        _require_no_extended_acl_fd(descriptor, code)
        if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != mode:
            raise ModelStoreError(code)
    finally:
        os.close(descriptor)


def _seal_regular_file(path: Path, mode: int, code: str) -> None:
    descriptor = _open_regular(path, code)
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise ModelStoreError(code)
        _clear_extended_acl_fd(descriptor, code)
        os.fchmod(descriptor, mode)
        status = os.fstat(descriptor)
        _require_no_extended_acl_fd(descriptor, code)
        if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != mode:
            raise ModelStoreError(code)
        os.fsync(descriptor)
    except OSError:
        raise ModelStoreError(code) from None
    finally:
        os.close(descriptor)


def _write_sealed_pointer_candidate(
    models_root: Path, value: Mapping[str, object]
) -> Path:
    candidate = models_root / f".current-{uuid.uuid4().hex}.tmp"
    try:
        atomic_write_json(candidate, value)
        _seal_regular_file(candidate, 0o400, "installed_pointer_invalid")
        _require_owned_mode(candidate, 0o400, "installed_pointer_invalid")
        if _read_canonical_mapping(candidate, "installed_pointer_invalid") != value:
            raise ModelStoreError("installed_pointer_invalid")
        return candidate
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise


def _path_has_file_identity(path: Path, expected: tuple[int, int]) -> bool:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise ModelStoreError("installed_pointer_invalid") from None
    return stat.S_ISREG(status.st_mode) and (status.st_dev, status.st_ino) == expected


def _prepare_state_directories(root: Path) -> tuple[Path, Path, Path]:
    try:
        root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ModelStoreError("state_root_invalid") from None
    if not resolved_root.is_dir():
        raise ModelStoreError("state_root_invalid")
    demo_root = _contained_directory(
        resolved_root / "recall-demo", resolved_root, create=True, seal=True
    )
    models_root = _contained_directory(
        demo_root / "models", resolved_root, create=True, seal=True
    )
    staging_root = _contained_directory(
        models_root / "staging", resolved_root, create=True, seal=True
    )
    installs_root = _contained_directory(
        models_root / "installs", resolved_root, create=True, seal=True
    )
    return models_root, staging_root, installs_root


def _load_state_directories(root: Path) -> tuple[Path, Path]:
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ModelStoreError("state_root_invalid") from None
    if not resolved_root.is_dir():
        raise ModelStoreError("state_root_invalid")
    demo_root = _contained_directory(
        resolved_root / "recall-demo", resolved_root, create=False, seal=False
    )
    models_root = _contained_directory(
        demo_root / "models", resolved_root, create=False, seal=False
    )
    _contained_directory(
        models_root / "staging", resolved_root, create=False, seal=False
    )
    installs_root = _contained_directory(
        models_root / "installs", resolved_root, create=False, seal=False
    )
    return models_root, installs_root


def _contained_directory(
    path: Path,
    resolved_root: Path,
    *,
    create: bool,
    seal: bool,
) -> Path:
    try:
        if create:
            path.mkdir(exist_ok=True)
        resolved = path.resolve(strict=True)
        status = resolved.lstat()
    except (OSError, RuntimeError):
        raise ModelStoreError("state_root_invalid") from None
    if (
        path.is_symlink()
        or not stat.S_ISDIR(status.st_mode)
        or not resolved.is_relative_to(resolved_root)
        or status.st_uid != os.geteuid()
    ):
        raise ModelStoreError("state_root_invalid")
    if seal:
        _seal_directory(resolved, 0o700, "state_root_invalid")
    else:
        _require_directory(resolved, 0o700, "state_root_invalid")
    return resolved


def _clone_snapshot(
    source_root: Path, destination_root: Path, spec: DemoModelSpec
) -> None:
    if not source_root.is_absolute() or not source_root.is_dir():
        raise ModelStoreError("source_snapshot_invalid")
    try:
        if not any(source_root.iterdir()):
            raise ModelStoreError("source_snapshot_invalid")
        destination_dir_fd = os.open(
            destination_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        raise ModelStoreError("source_snapshot_invalid") from None
    try:
        for binding in spec.files:
            source_path = source_root / binding.name
            try:
                resolved_source = source_path.resolve(strict=True)
                source_fd = os.open(
                    resolved_source,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except (OSError, RuntimeError):
                raise ModelStoreError("source_file_invalid") from None
            try:
                source_status = os.fstat(source_fd)
                if not stat.S_ISREG(source_status.st_mode):
                    raise ModelStoreError("source_file_invalid")
                if (
                    source_status.st_size != binding.size
                    or _sha256_fd(source_fd) != binding.sha256
                ):
                    raise ModelStoreError("source_file_digest_invalid")
                try:
                    _clone_file(source_fd, destination_dir_fd, binding.name)
                except OSError:
                    raise ModelStoreError("model_clone_unavailable") from None
                _validate_cloned_file(
                    source_status,
                    destination_dir_fd,
                    binding.name,
                    binding.size,
                    binding.sha256,
                )
            finally:
                os.close(source_fd)
    finally:
        os.close(destination_dir_fd)


def _clone_file(source_fd: int, destination_dir_fd: int, name: str) -> None:
    """Create one forced CoW clone; unsupported paths fail without fallback."""

    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        operation = getattr(libc, "fclonefileat", None)
        if operation is None:
            raise OSError(errno.ENOTSUP, "copy-on-write cloning is unavailable")
        operation.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        operation.restype = ctypes.c_int
        if (
            operation(
                source_fd,
                destination_dir_fd,
                os.fsencode(name),
                _CLONE_NOOWNERCOPY,
            )
            != 0
        ):
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        return
    if sys.platform.startswith("linux"):
        if fcntl is None:
            raise OSError(errno.ENOTSUP, "copy-on-write cloning is unavailable")
        request = _linux_ficlone_request(platform.machine())
        destination_fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=destination_dir_fd,
        )
        try:
            fcntl.ioctl(destination_fd, request, source_fd)
        except OSError:
            os.close(destination_fd)
            os.unlink(name, dir_fd=destination_dir_fd)
            raise
        os.close(destination_fd)
        return
    raise OSError(errno.ENOTSUP, "copy-on-write cloning is unavailable")


def _linux_ficlone_request(machine: str) -> int:
    normalized = machine.strip().lower()
    if normalized.startswith(("mips", "ppc", "powerpc", "sparc")):
        return _ALT_DIRECTION_FICLONE
    if (
        normalized in {"x86_64", "amd64", "aarch64", "arm64", "s390", "s390x"}
        or re.fullmatch(r"i[3-6]86", normalized) is not None
        or normalized.startswith(("arm", "riscv", "loong"))
    ):
        return _GENERIC_FICLONE
    raise OSError(errno.ENOTSUP, "copy-on-write cloning is unavailable")


def _validate_cloned_file(
    source_status: os.stat_result,
    destination_dir_fd: int,
    name: str,
    expected_size: int,
    expected_digest: str,
) -> None:
    try:
        destination_fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=destination_dir_fd,
        )
    except OSError:
        raise ModelStoreError("model_clone_unavailable") from None
    try:
        destination_status = os.fstat(destination_fd)
        if (
            not stat.S_ISREG(destination_status.st_mode)
            or (destination_status.st_dev, destination_status.st_ino)
            == (source_status.st_dev, source_status.st_ino)
        ):
            raise ModelStoreError("model_clone_unavailable")
        if destination_status.st_uid != os.geteuid():
            raise ModelStoreError("installed_permissions_invalid")
        if (
            destination_status.st_size != expected_size
            or _sha256_fd(destination_fd) != expected_digest
        ):
            raise ModelStoreError("installed_file_digest_invalid")
        _clear_extended_acl_fd(destination_fd, "installed_permissions_invalid")
        os.fchmod(destination_fd, 0o400)
        sealed_status = os.fstat(destination_fd)
        _require_no_extended_acl_fd(
            destination_fd, "installed_permissions_invalid"
        )
        if (
            sealed_status.st_uid != os.geteuid()
            or stat.S_IMODE(sealed_status.st_mode) != 0o400
        ):
            raise ModelStoreError("installed_permissions_invalid")
    except OSError:
        raise ModelStoreError("model_clone_unavailable") from None
    finally:
        os.close(destination_fd)


def _expected_models(profile: DemoRetrievalProfile) -> list[dict[str, object]]:
    return [
        {
            "role": "embedding",
            "model_id": profile.embedding.model_id,
            "revision": profile.embedding.revision,
        },
        {
            "role": "reranker",
            "model_id": profile.reranker.model_id,
            "revision": profile.reranker.revision,
        },
    ]


def _expected_file_records(
    profile: DemoRetrievalProfile,
) -> list[dict[str, object]]:
    records = [
        {
            "path": f"{role}/{binding.name}",
            "sha256": binding.sha256,
            "size": binding.size,
        }
        for role, spec in (
            ("embedding", profile.embedding),
            ("reranker", profile.reranker),
        )
        for binding in spec.files
    ]
    return sorted(records, key=lambda item: item["path"])


def _validate_install(
    install_path: Path,
    profile: DemoRetrievalProfile,
    expected_files: list[dict[str, object]],
    *,
    sealed: bool,
) -> None:
    _require_directory(
        install_path,
        0o500 if sealed else 0o700,
        "installed_permissions_invalid",
    )
    manifest_path = install_path / "model-install.json"
    _require_owned_mode(manifest_path, 0o400, "installed_manifest_invalid")
    manifest = _read_canonical_mapping(manifest_path, "installed_manifest_invalid")
    if (
        frozenset(manifest)
        != frozenset(
            (
                "schema_version",
                "materialization",
                "profile_digest",
                "models",
                "files",
            )
        )
        or not _is_schema_version(manifest["schema_version"], 2)
        or manifest["materialization"] != _MATERIALIZATION
    ):
        raise ModelStoreError("installed_manifest_invalid")
    if manifest["profile_digest"] != profile.digest:
        raise ModelStoreError("profile_digest_invalid")
    if manifest["models"] != _expected_models(profile):
        raise ModelStoreError("installed_manifest_invalid")
    _validated_file_records(manifest["files"])
    if manifest["files"] != expected_files:
        raise ModelStoreError("installed_manifest_invalid")
    _validate_actual_files(
        install_path, expected_files, sealed=sealed, include_manifest=True
    )


def _validate_install_metadata(
    install_path: Path, profile: DemoRetrievalProfile
) -> None:
    _require_directory(
        install_path, 0o500, "installed_permissions_invalid"
    )
    manifest_path = install_path / "model-install.json"
    _require_owned_mode(manifest_path, 0o400, "installed_manifest_invalid")
    manifest = _read_canonical_mapping(manifest_path, "installed_manifest_invalid")
    if (
        frozenset(manifest)
        != frozenset(
            (
                "schema_version",
                "materialization",
                "profile_digest",
                "models",
                "files",
            )
        )
        or not _is_schema_version(manifest["schema_version"], 2)
        or manifest["materialization"] != _MATERIALIZATION
        or manifest["profile_digest"] != profile.digest
        or manifest["models"] != _expected_models(profile)
    ):
        raise ModelStoreError("installed_manifest_invalid")
    _validated_file_records(manifest["files"])
    if manifest["files"] != _expected_file_records(profile):
        raise ModelStoreError("installed_manifest_invalid")
    _validate_metadata_file_set(install_path, profile)


def _validate_metadata_file_set(
    install_path: Path, profile: DemoRetrievalProfile
) -> None:
    expected_files = _expected_file_records(profile)
    expected_paths = {str(record["path"]) for record in expected_files}
    _validate_install_tree(
        install_path, expected_paths, sealed=True, include_manifest=True
    )
    for record in expected_files:
        descriptor = _open_regular(
            install_path / str(record["path"]), "installed_file_set_invalid"
        )
        try:
            status = os.fstat(descriptor)
            _require_no_extended_acl_fd(descriptor, "installed_permissions_invalid")
            if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o400:
                raise ModelStoreError("installed_permissions_invalid")
            if status.st_size != record["size"]:
                raise ModelStoreError("installed_file_digest_invalid")
        finally:
            os.close(descriptor)


def _validate_actual_files(
    install_path: Path,
    expected_files: list[dict[str, object]],
    *,
    sealed: bool,
    include_manifest: bool,
) -> None:
    expected_paths = {str(record["path"]) for record in expected_files}
    _validate_install_tree(
        install_path,
        expected_paths,
        sealed=sealed,
        include_manifest=include_manifest,
    )
    for record in expected_files:
        path = install_path / str(record["path"])
        descriptor = _open_regular(path, "installed_file_digest_invalid")
        try:
            status = os.fstat(descriptor)
            _require_no_extended_acl_fd(
                descriptor, "installed_permissions_invalid"
            )
            if sealed and (
                status.st_uid != os.geteuid()
                or stat.S_IMODE(status.st_mode) != 0o400
            ):
                raise ModelStoreError("installed_permissions_invalid")
            if (
                status.st_size != record["size"]
                or _sha256_fd(descriptor) != record["sha256"]
            ):
                raise ModelStoreError("installed_file_digest_invalid")
        finally:
            os.close(descriptor)


def _read_canonical_mapping(path: Path, code: str) -> Mapping[str, object]:
    descriptor = _open_regular(path, code)
    try:
        _require_no_extended_acl_fd(descriptor, code)
        content = _read_fd(descriptor)
        value = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ModelStoreError(code) from None
    finally:
        os.close(descriptor)
    if not isinstance(value, Mapping) or canonical_json_bytes(value) != content:
        raise ModelStoreError(code)
    return value


def _validated_file_records(value: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list):
        raise ModelStoreError("installed_file_set_invalid")
    records: dict[str, Mapping[str, object]] = {}
    ordered_paths: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or frozenset(item) != frozenset(
            ("path", "sha256", "size")
        ):
            raise ModelStoreError("installed_file_set_invalid")
        path = item["path"]
        digest = item["sha256"]
        size = item["size"]
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or path in records
        ):
            raise ModelStoreError("installed_file_set_invalid")
        ordered_paths.append(path)
        records[path] = item
    if ordered_paths != sorted(ordered_paths):
        raise ModelStoreError("installed_file_set_invalid")
    return records


def _validate_install_tree(
    install_path: Path,
    expected_paths: set[str],
    *,
    sealed: bool,
    include_manifest: bool,
) -> None:
    try:
        top_level = {path.name: path for path in install_path.iterdir()}
    except OSError:
        raise ModelStoreError("installed_file_set_invalid") from None
    expected_top_level = {"embedding", "reranker"}
    if include_manifest:
        expected_top_level.add("model-install.json")
    if set(top_level) != expected_top_level:
        raise ModelStoreError("installed_file_set_invalid")
    actual_paths: set[str] = set()
    for role in _ROLES:
        role_path = top_level[role]
        try:
            role_status = role_path.lstat()
            entries = tuple(role_path.iterdir())
        except OSError:
            raise ModelStoreError("installed_file_set_invalid") from None
        if role_path.is_symlink() or not stat.S_ISDIR(role_status.st_mode):
            raise ModelStoreError("installed_file_set_invalid")
        expected_mode = 0o500 if sealed else 0o700
        _require_directory(
            role_path, expected_mode, "installed_permissions_invalid"
        )
        for entry in entries:
            try:
                status = entry.lstat()
            except OSError:
                raise ModelStoreError("installed_file_set_invalid") from None
            if entry.is_symlink() or not stat.S_ISREG(status.st_mode):
                raise ModelStoreError("installed_file_set_invalid")
            actual_paths.add(f"{role}/{entry.name}")
    if actual_paths != expected_paths:
        raise ModelStoreError("installed_file_set_invalid")


def _require_owned_mode(path: Path, mode: int, code: str) -> None:
    try:
        status = path.lstat()
    except OSError:
        raise ModelStoreError(code) from None
    if (
        path.is_symlink()
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != mode
    ):
        raise ModelStoreError(code)


def _open_regular(path: Path, code: str) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        status = os.fstat(descriptor)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise ModelStoreError(code) from None
    if not stat.S_ISREG(status.st_mode):
        os.close(descriptor)
        raise ModelStoreError(code)
    return descriptor


def _read_fd(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _sha256_fd(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _remove_staging_generation(generation: Path, staging_root: Path) -> None:
    if generation.parent != staging_root or not re.fullmatch(r"[0-9a-f]{32}", generation.name):
        raise ModelStoreError("state_root_invalid")
    for directory, child_directories, _files in os.walk(
        generation, topdown=True, followlinks=False
    ):
        for path in (Path(directory), *(Path(directory) / name for name in child_directories)):
            try:
                status = path.lstat()
                if stat.S_ISDIR(status.st_mode) and status.st_uid == os.geteuid():
                    os.chmod(path, 0o700)
            except OSError:
                raise ModelStoreError("state_root_invalid") from None
    shutil.rmtree(generation)


def _remove_uncommitted_install(
    install_path: Path,
    installs_root: Path,
    profile_digest: str,
    expected_identity: tuple[int, int],
) -> None:
    allowed_name = re.fullmatch(
        rf"{re.escape(profile_digest)}(?:-[0-9a-f]{{32}})?",
        install_path.name,
    )
    if install_path.parent != installs_root or allowed_name is None:
        raise ModelStoreError("state_root_invalid")
    try:
        status = install_path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ModelStoreError("state_root_invalid") from None
    if (status.st_dev, status.st_ino) != expected_identity:
        return
    if (
        install_path.is_symlink()
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
    ):
        raise ModelStoreError("state_root_invalid")
    for directory, child_directories, _files in os.walk(
        install_path, topdown=True, followlinks=False
    ):
        for path in (
            Path(directory),
            *(Path(directory) / name for name in child_directories),
        ):
            try:
                child_status = path.lstat()
                if (
                    stat.S_ISDIR(child_status.st_mode)
                    and child_status.st_uid == os.geteuid()
                ):
                    os.chmod(path, 0o700)
            except OSError:
                raise ModelStoreError("state_root_invalid") from None
    shutil.rmtree(install_path)
    _fsync_directory(installs_root)


def _sync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            _fsync_file(path)
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()), reverse=True
    ):
        _fsync_directory(path)
    _fsync_directory(root)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
