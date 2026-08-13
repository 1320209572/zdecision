"""Build and verify the public-only frozen recall demonstration bundle."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.models import DecisionRevision, ProductMetadata, ProductRegistry

from zdecision.recall.demo.contracts import DemoRetrievalProfile


_BUNDLE_FILES = frozenset(
    ("snapshot.json", "retrieval-profile.json", "signed-manifest.json")
)
_SNAPSHOT_FIELDS = frozenset(
    ("schema_version", "decision_space_id", "product_name", "repository", "decisions")
)
_MANIFEST_FIELDS = frozenset(
    ("schema_version", "files", "decision_count", "decision_leaves")
)
_SIGNED_MANIFEST_FIELDS = frozenset(("key_id", "manifest", "signature"))
_MIN_ACTIVE_DECISIONS = 1
_MAX_ACTIVE_DECISIONS = 32


class DemoBundleError(RuntimeError):
    """A deliberately non-sensitive bundle validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VerifiedDemoBundle:
    decision_space_id: str
    product_name: str
    repository: str
    profile: DemoRetrievalProfile
    decisions: tuple[DecisionRevision, ...]
    manifest_digest: str


@dataclass(frozen=True)
class VerifiedDemoBundleMetadata:
    """Signed bundle facts safe to use before consent-time retrieval."""

    decision_space_id: str
    product_name: str
    repository: str
    profile: DemoRetrievalProfile
    manifest_digest: str
    decision_count: int
    decision_leaves: tuple[tuple[str, int], ...]


def build_signed_bundle(
    *,
    product_root: Path,
    profile_path: Path,
    private_key_path: Path,
    key_id: str,
    output_root: Path,
) -> Path:
    """Produce a signed immutable bundle, without copying the signing key."""
    product_root = Path(product_root)
    profile_path = Path(profile_path)
    private_key_path = Path(private_key_path)
    output_root = Path(output_root)
    try:
        if private_key_path.resolve().is_relative_to(output_root.resolve()):
            raise DemoBundleError("private_key_location")
        if output_root.exists():
            raise DemoBundleError("output_exists")
        if not isinstance(key_id, str) or not key_id:
            raise DemoBundleError("key_id_invalid")

        product_value = _read_canonical_json(product_root / "product.json")
        registry_value = _read_canonical_json(product_root / "registry.json")
        if not isinstance(product_value, Mapping) or not isinstance(registry_value, Mapping):
            raise DemoBundleError("source_invalid")
        product = ProductMetadata.from_dict(product_value)
        registry = ProductRegistry.from_dict(registry_value)
        if registry.product_id != product.product_id:
            raise DemoBundleError("source_invalid")

        profile_value = _read_canonical_json(profile_path)
        profile = DemoRetrievalProfile.from_dict(profile_value)
        if (
            profile.decision_space_id != product.product_id
            or profile.product_name != product.name
        ):
            raise DemoBundleError("profile_invalid")

        decisions = _read_product_heads(product_root, product, registry, profile.repository)
        snapshot = {
            "schema_version": 1,
            "decision_space_id": product.product_id,
            "product_name": product.name,
            "repository": profile.repository,
            "decisions": [decision.to_dict() for decision in decisions],
        }
        snapshot_bytes = canonical_json_bytes(snapshot)
        profile_bytes = canonical_json_bytes(profile.to_dict())
        manifest = _manifest(snapshot_bytes, profile_bytes, decisions)
        signature = _private_key(private_key_path).sign(canonical_json_bytes(manifest))
        signed_manifest = {
            "key_id": key_id,
            "manifest": manifest,
            "signature": base64.b64encode(signature).decode("ascii"),
        }
        return _write_bundle(
            output_root,
            {
                "snapshot.json": snapshot_bytes,
                "retrieval-profile.json": profile_bytes,
                "signed-manifest.json": canonical_json_bytes(signed_manifest),
            },
        )
    except DemoBundleError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise DemoBundleError("source_invalid") from None


def load_verified_bundle(
    *, bundle_root: Path, trust_root_path: Path
) -> VerifiedDemoBundle:
    """Verify a signed bundle using only a raw external public trust root."""
    bundle_root = Path(bundle_root)
    trust_root_path = Path(trust_root_path)
    try:
        if not bundle_root.is_dir() or frozenset(path.name for path in bundle_root.iterdir()) != _BUNDLE_FILES:
            raise DemoBundleError("bundle_invalid")
        signed = _read_canonical_json(bundle_root / "signed-manifest.json")
        if not isinstance(signed, Mapping) or frozenset(signed) != _SIGNED_MANIFEST_FIELDS:
            raise DemoBundleError("manifest_invalid")
        key_id = signed["key_id"]
        manifest = signed["manifest"]
        signature_value = signed["signature"]
        if not isinstance(key_id, str) or not key_id or not isinstance(manifest, Mapping) or not isinstance(signature_value, str):
            raise DemoBundleError("manifest_invalid")
        try:
            signature = base64.b64decode(signature_value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError):
            raise DemoBundleError("manifest_invalid") from None
        try:
            _public_key(trust_root_path).verify(signature, canonical_json_bytes(manifest))
        except InvalidSignature:
            raise DemoBundleError("signature_invalid") from None

        _validate_manifest_shape(manifest)
        snapshot_bytes = _read_bound_payload(bundle_root / "snapshot.json", manifest, "snapshot.json")
        profile_bytes = _read_bound_payload(
            bundle_root / "retrieval-profile.json", manifest, "retrieval-profile.json"
        )
        snapshot = _decode_canonical_bytes(snapshot_bytes)
        profile_value = _decode_canonical_bytes(profile_bytes)
        profile = DemoRetrievalProfile.from_dict(profile_value)
        decisions = _validate_snapshot(snapshot, profile, manifest)
        return VerifiedDemoBundle(
            decision_space_id=profile.decision_space_id,
            product_name=profile.product_name,
            repository=profile.repository,
            profile=profile,
            decisions=decisions,
            manifest_digest=hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
        )
    except DemoBundleError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise DemoBundleError("bundle_invalid") from None


def load_verified_bundle_metadata(
    *, bundle_root: Path, trust_root_path: Path
) -> VerifiedDemoBundleMetadata:
    """Verify signed, bounded bundle metadata without returning Decision prose."""
    bundle_root = Path(bundle_root)
    trust_root_path = Path(trust_root_path)
    try:
        if not bundle_root.is_dir() or frozenset(path.name for path in bundle_root.iterdir()) != _BUNDLE_FILES:
            raise DemoBundleError("bundle_invalid")
        signed = _read_canonical_json(bundle_root / "signed-manifest.json")
        if not isinstance(signed, Mapping) or frozenset(signed) != _SIGNED_MANIFEST_FIELDS:
            raise DemoBundleError("manifest_invalid")
        key_id = signed["key_id"]
        manifest = signed["manifest"]
        signature_value = signed["signature"]
        if not isinstance(key_id, str) or not key_id or not isinstance(manifest, Mapping) or not isinstance(signature_value, str):
            raise DemoBundleError("manifest_invalid")
        try:
            signature = base64.b64decode(signature_value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError):
            raise DemoBundleError("manifest_invalid") from None
        try:
            _public_key(trust_root_path).verify(signature, canonical_json_bytes(manifest))
        except InvalidSignature:
            raise DemoBundleError("signature_invalid") from None

        _validate_manifest_shape(manifest)
        snapshot_bytes = _read_bound_payload(bundle_root / "snapshot.json", manifest, "snapshot.json")
        profile_bytes = _read_bound_payload(
            bundle_root / "retrieval-profile.json", manifest, "retrieval-profile.json"
        )
        snapshot = _decode_canonical_bytes(snapshot_bytes)
        profile = DemoRetrievalProfile.from_dict(_decode_canonical_bytes(profile_bytes))
        decision_count, decision_leaves = _validate_snapshot_metadata(
            snapshot, profile, manifest
        )
        return VerifiedDemoBundleMetadata(
            decision_space_id=profile.decision_space_id,
            product_name=profile.product_name,
            repository=profile.repository,
            profile=profile,
            manifest_digest=hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
            decision_count=decision_count,
            decision_leaves=decision_leaves,
        )
    except DemoBundleError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise DemoBundleError("bundle_invalid") from None


def _read_product_heads(
    product_root: Path,
    product: ProductMetadata,
    registry: ProductRegistry,
    repository: str,
) -> tuple[DecisionRevision, ...]:
    decisions: list[DecisionRevision] = []
    for decision_id, head in registry.decisions.items():
        if head.lifecycle != "active":
            continue
        if not _is_positive_integer(head.head_revision):
            raise DemoBundleError("source_invalid")
        value = _read_canonical_json(product_root / head.head_path)
        if not isinstance(value, Mapping):
            raise DemoBundleError("source_invalid")
        decision = DecisionRevision.from_dict(value)
        _validate_leaf(decision, product.product_id, product.name, repository)
        if decision.decision_id != decision_id or decision.revision != head.head_revision:
            raise DemoBundleError("source_invalid")
        decisions.append(decision)
    if tuple(decision.decision_id for decision in decisions) != tuple(
        sorted(decision.decision_id for decision in decisions)
    ) or not _valid_active_count(len(decisions)):
        raise DemoBundleError("source_invalid")
    return tuple(decisions)


def _manifest(
    snapshot_bytes: bytes,
    profile_bytes: bytes,
    decisions: tuple[DecisionRevision, ...],
) -> dict[str, object]:
    if not _valid_active_count(len(decisions)):
        raise DemoBundleError("source_invalid")
    return {
        "schema_version": 1,
        "files": {
            "snapshot.json": _file_binding(snapshot_bytes),
            "retrieval-profile.json": _file_binding(profile_bytes),
        },
        "decision_count": len(decisions),
        "decision_leaves": [
            {"decision_id": decision.decision_id, "revision": decision.revision}
            for decision in decisions
        ],
    }


def _file_binding(content: bytes) -> dict[str, object]:
    return {"sha256": hashlib.sha256(content).hexdigest(), "byte_length": len(content)}


def _write_bundle(output_root: Path, files: Mapping[str, bytes]) -> Path:
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    try:
        os.chmod(temporary, 0o700)
        for name, content in files.items():
            path = temporary / name
            path.write_bytes(content)
            os.chmod(path, 0o600)
        if frozenset(path.name for path in temporary.iterdir()) != _BUNDLE_FILES:
            raise DemoBundleError("bundle_invalid")
        if output_root.exists():
            raise DemoBundleError("output_exists")
        try:
            _rename_no_replace(temporary, output_root)
        except FileExistsError:
            raise DemoBundleError("output_exists") from None
        return output_root
    except DemoBundleError:
        raise
    except OSError:
        raise DemoBundleError("bundle_invalid") from None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename a sibling directory only when *destination* is absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation = libc.renameatx_np
        flags = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        operation = getattr(libc, "renameat2", None)
        if operation is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        flags = 0x00000001  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    operation.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    operation.restype = ctypes.c_int
    status = operation(
        -1,
        os.fsencode(source.resolve()),
        -1,
        os.fsencode(destination.resolve()),
        flags,
    )
    if status != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _private_key(path: Path) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def _public_key(path: Path) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(path.read_bytes())


def _read_canonical_json(path: Path) -> object:
    content = path.read_bytes()
    value = _decode_canonical_bytes(content)
    return value


def _decode_canonical_bytes(content: bytes) -> object:
    value = json.loads(content)
    if canonical_json_bytes(value) != content:
        raise DemoBundleError("canonical_json_invalid")
    return value


def _validate_manifest_shape(manifest: Mapping[str, object]) -> None:
    if (
        frozenset(manifest) != _MANIFEST_FIELDS
        or not _is_exact_integer(manifest["schema_version"], 1)
    ):
        raise DemoBundleError("manifest_invalid")
    files = manifest["files"]
    if not isinstance(files, Mapping) or frozenset(files) != {"snapshot.json", "retrieval-profile.json"}:
        raise DemoBundleError("manifest_invalid")
    for name in files:
        binding = files[name]
        if not isinstance(binding, Mapping) or frozenset(binding) != {"sha256", "byte_length"}:
            raise DemoBundleError("manifest_invalid")
        digest = binding["sha256"]
        size = binding["byte_length"]
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise DemoBundleError("manifest_invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DemoBundleError("manifest_invalid")
    count = manifest["decision_count"]
    leaves = manifest["decision_leaves"]
    if (
        not _valid_active_count(count)
        or not isinstance(leaves, list)
        or len(leaves) != count
    ):
        raise DemoBundleError("manifest_invalid")
    identities: list[str] = []
    for leaf in leaves:
        if (
            not isinstance(leaf, Mapping)
            or frozenset(leaf) != {"decision_id", "revision"}
            or not isinstance(leaf["decision_id"], str)
            or not _is_positive_integer(leaf["revision"])
        ):
            raise DemoBundleError("manifest_invalid")
        identities.append(leaf["decision_id"])
    if identities != sorted(identities) or len(set(identities)) != len(identities):
        raise DemoBundleError("manifest_invalid")


def _read_bound_payload(path: Path, manifest: Mapping[str, object], name: str) -> bytes:
    content = path.read_bytes()
    binding = manifest["files"]
    assert isinstance(binding, Mapping)
    item = binding[name]
    assert isinstance(item, Mapping)
    if len(content) != item["byte_length"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
        raise DemoBundleError("payload_invalid")
    return content


def _validate_snapshot(
    snapshot: object,
    profile: DemoRetrievalProfile,
    manifest: Mapping[str, object],
) -> tuple[DecisionRevision, ...]:
    if not isinstance(snapshot, Mapping) or frozenset(snapshot) != _SNAPSHOT_FIELDS:
        raise DemoBundleError("snapshot_invalid")
    if (
        not _is_exact_integer(snapshot["schema_version"], 1)
        or snapshot["decision_space_id"] != profile.decision_space_id
        or snapshot["product_name"] != profile.product_name
        or snapshot["repository"] != profile.repository
        or not isinstance(snapshot["decisions"], list)
    ):
        raise DemoBundleError("snapshot_invalid")
    decisions: list[DecisionRevision] = []
    for value in snapshot["decisions"]:
        if not isinstance(value, Mapping):
            raise DemoBundleError("snapshot_invalid")
        try:
            decision = DecisionRevision.from_dict(value)
        except ValueError:
            raise DemoBundleError("snapshot_invalid") from None
        _validate_leaf(
            decision,
            profile.decision_space_id,
            profile.product_name,
            profile.repository,
        )
        decisions.append(decision)
    identities = [(item.decision_id, item.revision) for item in decisions]
    expected = [
        (leaf["decision_id"], leaf["revision"])
        for leaf in manifest["decision_leaves"]
    ]
    if (
        not _valid_active_count(len(decisions))
        or len(decisions) != manifest["decision_count"]
        or identities != expected
        or [item.decision_id for item in decisions] != sorted(item.decision_id for item in decisions)
        or len({item.decision_id for item in decisions}) != len(decisions)
    ):
        raise DemoBundleError("snapshot_invalid")
    return tuple(decisions)


def _validate_snapshot_metadata(
    snapshot: object,
    profile: DemoRetrievalProfile,
    manifest: Mapping[str, object],
) -> tuple[int, tuple[tuple[str, int], ...]]:
    """Confirm snapshot identity/count/leaves while keeping Decisions opaque."""
    if not isinstance(snapshot, Mapping) or frozenset(snapshot) != _SNAPSHOT_FIELDS:
        raise DemoBundleError("snapshot_invalid")
    if (
        not _is_exact_integer(snapshot["schema_version"], 1)
        or snapshot["decision_space_id"] != profile.decision_space_id
        or snapshot["product_name"] != profile.product_name
        or snapshot["repository"] != profile.repository
        or not isinstance(snapshot["decisions"], list)
    ):
        raise DemoBundleError("snapshot_invalid")
    leaves: list[tuple[str, int]] = []
    for value in snapshot["decisions"]:
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("decision_id"), str)
            or not _is_positive_integer(value.get("revision"))
        ):
            raise DemoBundleError("snapshot_invalid")
        leaves.append((value["decision_id"], value["revision"]))
    manifest_leaves = manifest["decision_leaves"]
    assert isinstance(manifest_leaves, list)
    expected = tuple(
        (leaf["decision_id"], leaf["revision"])
        for leaf in manifest_leaves
        if isinstance(leaf, Mapping)
    )
    if (
        not _valid_active_count(len(leaves))
        or len(leaves) != manifest["decision_count"]
        or tuple(leaves) != expected
        or [decision_id for decision_id, _revision in leaves]
        != sorted(decision_id for decision_id, _revision in leaves)
    ):
        raise DemoBundleError("snapshot_invalid")
    return len(leaves), tuple(leaves)


def _is_exact_integer(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_active_count(value: int) -> bool:
    return _MIN_ACTIVE_DECISIONS <= value <= _MAX_ACTIVE_DECISIONS


def _validate_leaf(
    decision: DecisionRevision,
    decision_space_id: str,
    product_name: str,
    repository: str,
) -> None:
    if (
        decision.product_id != decision_space_id
        or decision.product_name != product_name
        or decision.lifecycle != "active"
        or not _is_positive_integer(decision.revision)
        or decision.repositories != (repository,)
    ):
        raise DemoBundleError("snapshot_invalid")
