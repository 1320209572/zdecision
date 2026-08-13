"""Owner-only local configuration for the bounded Recall demonstration."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import private_state_root


_FIELDS = frozenset(
    (
        "schema_version",
        "repository_name",
        "product_name",
        "decision_space_id",
        "registry_product_root",
        "profile_path",
        "model_state_root",
        "trust_root_path",
        "bundle_state_root",
        "signing_private_key_path",
        "signing_key_id",
    )
)
_REPOSITORY_NAME = "zstack-ui-next"
_PRODUCT_NAME = "third-party-services"
_DECISION_SPACE_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
_SIGNING_KEY_ID = "demo-leadership-v1"


@dataclass(frozen=True)
class DemoProviderConfig:
    repository_name: str
    product_name: str
    decision_space_id: str
    profile_path: Path
    model_state_root: Path
    trust_root_path: Path
    bundle_state_root: Path


@dataclass(frozen=True)
class DemoPublisherConfig:
    provider: DemoProviderConfig
    registry_product_root: Path
    signing_private_key_path: Path
    signing_key_id: str


@dataclass(frozen=True)
class DemoRecallConfig:
    schema_version: Literal[1]
    provider: DemoProviderConfig
    publisher: DemoPublisherConfig

    @classmethod
    def from_dict(cls, value: object) -> "DemoRecallConfig":
        if not isinstance(value, Mapping) or frozenset(value) != _FIELDS:
            raise ValueError("recall_demo_config_invalid")
        if (
            value["schema_version"] != 1
            or isinstance(value["schema_version"], bool)
            or value["repository_name"] != _REPOSITORY_NAME
            or value["product_name"] != _PRODUCT_NAME
            or value["decision_space_id"] != _DECISION_SPACE_ID
            or value["signing_key_id"] != _SIGNING_KEY_ID
        ):
            raise ValueError("recall_demo_config_invalid")
        paths = {
            name: _absolute_path(value[name])
            for name in (
                "registry_product_root",
                "profile_path",
                "model_state_root",
                "trust_root_path",
                "bundle_state_root",
                "signing_private_key_path",
            )
        }
        provider = DemoProviderConfig(
            repository_name=_REPOSITORY_NAME,
            product_name=_PRODUCT_NAME,
            decision_space_id=_DECISION_SPACE_ID,
            profile_path=paths["profile_path"],
            model_state_root=paths["model_state_root"],
            trust_root_path=paths["trust_root_path"],
            bundle_state_root=paths["bundle_state_root"],
        )
        return cls(
            schema_version=1,
            provider=provider,
            publisher=DemoPublisherConfig(
                provider=provider,
                registry_product_root=paths["registry_product_root"],
                signing_private_key_path=paths["signing_private_key_path"],
                signing_key_id=_SIGNING_KEY_ID,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "repository_name": self.provider.repository_name,
            "product_name": self.provider.product_name,
            "decision_space_id": self.provider.decision_space_id,
            "registry_product_root": str(self.publisher.registry_product_root),
            "profile_path": str(self.provider.profile_path),
            "model_state_root": str(self.provider.model_state_root),
            "trust_root_path": str(self.provider.trust_root_path),
            "bundle_state_root": str(self.provider.bundle_state_root),
            "signing_private_key_path": str(self.publisher.signing_private_key_path),
            "signing_key_id": self.publisher.signing_key_id,
        }


def recall_demo_config_path(environ: Mapping[str, str]) -> Path:
    return private_state_root(environ) / "agent" / "recall-demo.json"


def load_demo_recall_config(path: Path) -> DemoRecallConfig:
    path = Path(path)
    try:
        state = path.lstat()
        if (
            stat.S_ISLNK(state.st_mode)
            or not stat.S_ISREG(state.st_mode)
            or state.st_uid != os.getuid()
            or stat.S_IMODE(state.st_mode) != 0o600
        ):
            raise ValueError("recall_demo_config_invalid")
        return DemoRecallConfig.from_dict(json.loads(path.read_bytes()))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("recall_demo_config_invalid") from None


def write_demo_recall_config(path: Path, config: DemoRecallConfig) -> None:
    if not isinstance(config, DemoRecallConfig):
        raise ValueError("recall_demo_config_invalid")
    DemoRecallConfig.from_dict(config.to_dict())
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            payload = canonical_json_bytes(config.to_dict())
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        path.chmod(0o600)
    except OSError:
        raise ValueError("recall_demo_config_invalid") from None


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError("recall_demo_config_invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("recall_demo_config_invalid")
    return path
