"""Immutable signed-bundle publication for the bounded Recall demonstration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.demo.bundle import (
    DemoBundleError,
    _rename_no_replace,
    build_signed_bundle,
    load_verified_bundle,
)
from zdecision.recall.demo.config import DemoProviderConfig, DemoPublisherConfig
from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.model_store import ModelStoreError, load_installed_models


_POINTER_FIELDS = frozenset(
    (
        "schema_version",
        "generation",
        "publication_commit",
        "bundle",
        "manifest_digest",
        "profile_digest",
        "model_install_digest",
        "generation_digest",
    )
)
_GENERATION_FIELDS = frozenset(
    (
        "schema_version",
        "publication_commit",
        "bundle",
        "manifest_digest",
        "profile_digest",
        "model_install_digest",
    )
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class RecallDemoPublicationError(RuntimeError):
    """A deliberately non-sensitive publication failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DemoBundlePointer:
    schema_version: int
    generation: int
    publication_commit: str
    bundle: str
    manifest_digest: str
    profile_digest: str
    model_install_digest: str
    generation_digest: str

    @classmethod
    def from_dict(cls, value: object) -> "DemoBundlePointer":
        if not isinstance(value, Mapping) or frozenset(value) != _POINTER_FIELDS:
            raise RecallDemoPublicationError("pointer_invalid")
        pointer = cls(
            schema_version=value["schema_version"],
            generation=value["generation"],
            publication_commit=value["publication_commit"],
            bundle=value["bundle"],
            manifest_digest=value["manifest_digest"],
            profile_digest=value["profile_digest"],
            model_install_digest=value["model_install_digest"],
            generation_digest=value["generation_digest"],
        )
        if (
            not _exact_integer(pointer.schema_version, 1)
            or not _positive_integer(pointer.generation)
            or _COMMIT.fullmatch(pointer.publication_commit) is None
            or pointer.bundle != f"bundles/{pointer.publication_commit}/bundle"
            or any(
                _DIGEST.fullmatch(digest) is None
                for digest in (
                    pointer.manifest_digest,
                    pointer.profile_digest,
                    pointer.model_install_digest,
                    pointer.generation_digest,
                )
            )
            or pointer.generation_digest != _pointer_digest(pointer)
        ):
            raise RecallDemoPublicationError("pointer_invalid")
        return pointer

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "publication_commit": self.publication_commit,
            "bundle": self.bundle,
            "manifest_digest": self.manifest_digest,
            "profile_digest": self.profile_digest,
            "model_install_digest": self.model_install_digest,
            "generation_digest": self.generation_digest,
        }


def load_demo_bundle_pointer(config: DemoProviderConfig) -> DemoBundlePointer:
    """Load one sealed current pointer without following an untrusted link."""
    try:
        root = _state_root(config.bundle_state_root)
        path = root / "current.json"
        state = path.lstat()
        if (
            stat.S_ISLNK(state.st_mode)
            or not stat.S_ISREG(state.st_mode)
            or state.st_uid != os.geteuid()
            or stat.S_IMODE(state.st_mode) != 0o600
        ):
            raise RecallDemoPublicationError("pointer_invalid")
        pointer = DemoBundlePointer.from_dict(_read_canonical_mapping(path))
        _verify_generation(
            root,
            pointer,
            _profile(config),
            _prepared_model_digest(config),
            config.trust_root_path,
        )
        return pointer
    except FileNotFoundError:
        raise
    except RecallDemoPublicationError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise RecallDemoPublicationError("pointer_invalid") from None


class DemoBundlePublisher:
    def __init__(self, config: DemoPublisherConfig) -> None:
        self._config = config

    def _load_current(self) -> DemoBundlePointer:
        return load_demo_bundle_pointer(self._config.provider)

    def refresh(self, publication_commit: str) -> DemoBundlePointer:
        """Build, verify, atomically retain, then select one completed commit."""
        if not isinstance(publication_commit, str) or _COMMIT.fullmatch(publication_commit) is None:
            raise RecallDemoPublicationError("publication_commit_invalid")
        try:
            root = _state_root(self._config.provider.bundle_state_root)
            bundles = root / "bundles"
            _ensure_owner_directory(root)
            _ensure_owner_directory(bundles)
            profile = _profile(self._config.provider)
            model_digest = _prepared_model_digest(self._config.provider)
            previous = self._current_or_none()
            if previous is not None and previous.publication_commit == publication_commit:
                return previous

            generation = bundles / publication_commit
            if generation.exists() or generation.is_symlink():
                metadata = _verify_existing_generation(
                    root, generation, publication_commit, profile, model_digest,
                    self._config.provider.trust_root_path,
                )
            else:
                metadata = self._create_generation(
                    bundles, generation, publication_commit, profile, model_digest
                )
            next_pointer = _pointer_from_generation(
                previous=previous, metadata=metadata
            )
            _replace_pointer(root / "current.json", next_pointer)
            return next_pointer
        except RecallDemoPublicationError:
            raise
        except (DemoBundleError, ModelStoreError):
            raise RecallDemoPublicationError("publication_invalid") from None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raise RecallDemoPublicationError("publication_invalid") from None
        except Exception:
            raise RecallDemoPublicationError("publication_invalid") from None

    def _current_or_none(self) -> DemoBundlePointer | None:
        try:
            return load_demo_bundle_pointer(self._config.provider)
        except FileNotFoundError:
            return None

    def _create_generation(
        self,
        bundles: Path,
        generation: Path,
        publication_commit: str,
        profile: DemoRetrievalProfile,
        model_digest: str,
    ) -> Mapping[str, object]:
        staging = bundles / f".{publication_commit}.{uuid.uuid4().hex}"
        try:
            staging.mkdir(mode=0o700)
            bundle = build_signed_bundle(
                product_root=self._config.registry_product_root,
                profile_path=self._config.provider.profile_path,
                private_key_path=self._config.signing_private_key_path,
                key_id=self._config.signing_key_id,
                output_root=staging / "bundle",
            )
            verified = load_verified_bundle(
                bundle_root=bundle, trust_root_path=self._config.provider.trust_root_path
            )
            metadata = _generation_metadata(
                publication_commit, verified.manifest_digest, profile.digest, model_digest
            )
            _write_sealed_json(staging / "generation.json", metadata)
            _sync_directory(staging)
            try:
                _rename_no_replace(staging, generation)
            except FileExistsError:
                return _verify_existing_generation(
                    bundles.parent, generation, publication_commit, profile, model_digest,
                    self._config.provider.trust_root_path,
                )
            _sync_directory(bundles)
            return metadata
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _pointer_digest(pointer: DemoBundlePointer) -> str:
    value = pointer.to_dict()
    value.pop("generation_digest")
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _pointer_from_generation(
    *, previous: DemoBundlePointer | None, metadata: Mapping[str, object]
) -> DemoBundlePointer:
    generation = 1 if previous is None else previous.generation + 1
    unsigned = DemoBundlePointer(
        schema_version=1,
        generation=generation,
        publication_commit=str(metadata["publication_commit"]),
        bundle=str(metadata["bundle"]),
        manifest_digest=str(metadata["manifest_digest"]),
        profile_digest=str(metadata["profile_digest"]),
        model_install_digest=str(metadata["model_install_digest"]),
        generation_digest="0" * 64,
    )
    return DemoBundlePointer(
        **{**unsigned.__dict__, "generation_digest": _pointer_digest(unsigned)}
    )


def _generation_metadata(
    publication_commit: str, manifest_digest: str, profile_digest: str, model_digest: str
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "publication_commit": publication_commit,
        "bundle": f"bundles/{publication_commit}/bundle",
        "manifest_digest": manifest_digest,
        "profile_digest": profile_digest,
        "model_install_digest": model_digest,
    }


def _verify_existing_generation(
    root: Path,
    generation: Path,
    publication_commit: str,
    profile: DemoRetrievalProfile,
    model_digest: str,
    trust_root: Path,
) -> Mapping[str, object]:
    try:
        state = generation.lstat()
    except OSError:
        raise RecallDemoPublicationError("generation_conflict") from None
    if (
        stat.S_ISLNK(state.st_mode)
        or not stat.S_ISDIR(state.st_mode)
        or state.st_uid != os.geteuid()
        or stat.S_IMODE(state.st_mode) != 0o700
    ):
        raise RecallDemoPublicationError("generation_conflict")
    metadata_path = generation / "generation.json"
    try:
        _require_owned_file(metadata_path, 0o600, "generation_conflict")
        metadata = _read_canonical_mapping(metadata_path)
    except RecallDemoPublicationError:
        raise RecallDemoPublicationError("generation_conflict") from None
    if frozenset(metadata) != _GENERATION_FIELDS:
        raise RecallDemoPublicationError("generation_conflict")
    expected = _generation_metadata(
        publication_commit,
        str(metadata.get("manifest_digest", "")),
        profile.digest,
        model_digest,
    )
    if metadata != expected:
        raise RecallDemoPublicationError("generation_conflict")
    try:
        verified = load_verified_bundle(
            bundle_root=generation / "bundle", trust_root_path=trust_root
        )
    except DemoBundleError:
        raise RecallDemoPublicationError("generation_conflict") from None
    if verified.manifest_digest != metadata["manifest_digest"]:
        raise RecallDemoPublicationError("generation_conflict")
    return metadata


def _verify_generation(
    root: Path,
    pointer: DemoBundlePointer,
    profile: DemoRetrievalProfile,
    model_digest: str,
    trust_root: Path,
) -> None:
    metadata = _verify_existing_generation(
        root,
        root / "bundles" / pointer.publication_commit,
        pointer.publication_commit,
        profile,
        model_digest,
        trust_root,
    )
    if any(pointer.to_dict()[name] != metadata[name] for name in _GENERATION_FIELDS - {"schema_version"}):
        raise RecallDemoPublicationError("pointer_invalid")


def _profile(config: DemoProviderConfig) -> DemoRetrievalProfile:
    return DemoRetrievalProfile.from_dict(_read_canonical_mapping(config.profile_path))


def _prepared_model_digest(config: DemoProviderConfig) -> str:
    profile = _profile(config)
    installed = load_installed_models(profile, config.model_state_root)
    return hashlib.sha256(installed.install_manifest_path.read_bytes()).hexdigest()


def _state_root(path: Path) -> Path:
    root = Path(path)
    if not root.is_absolute():
        raise RecallDemoPublicationError("pointer_invalid")
    return root


def _read_canonical_mapping(path: Path) -> Mapping[str, object]:
    content = path.read_bytes()
    value = json.loads(content)
    if not isinstance(value, Mapping) or canonical_json_bytes(value) != content:
        raise RecallDemoPublicationError("pointer_invalid")
    return value


def _write_sealed_json(path: Path, value: Mapping[str, object]) -> None:
    _write_owner_file(path, canonical_json_bytes(value))


def _replace_pointer(path: Path, pointer: DemoBundlePointer) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        _write_owner_file(temporary, canonical_json_bytes(pointer.to_dict()))
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_owner_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_owner_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = path.lstat()
    if (
        stat.S_ISLNK(state.st_mode)
        or not stat.S_ISDIR(state.st_mode)
        or state.st_uid != os.geteuid()
        or stat.S_IMODE(state.st_mode) != 0o700
    ):
        raise RecallDemoPublicationError("publication_invalid")


def _require_owned_file(path: Path, mode: int, code: str) -> None:
    state = path.lstat()
    if (
        stat.S_ISLNK(state.st_mode)
        or not stat.S_ISREG(state.st_mode)
        or state.st_uid != os.geteuid()
        or stat.S_IMODE(state.st_mode) != mode
    ):
        raise RecallDemoPublicationError(code)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exact_integer(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
