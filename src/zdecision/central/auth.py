"""Replaceable authentication boundary for the technical-loop demo."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Literal


PrincipalKind = Literal["user", "device"]

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class InvalidCredentials(Exception):
    """A stable, detail-free authentication failure."""

    def __init__(self, code: str = "device_authentication_failed") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    organization_id: str
    actor_id: str
    device_id: str | None

    def __post_init__(self) -> None:
        if self.kind not in ("user", "device"):
            raise ValueError("Principal kind is invalid")
        require_id(self.organization_id, "organization_id")
        require_id(self.actor_id, "actor_id")
        if self.kind == "user":
            if self.device_id is not None:
                raise ValueError("User Principal cannot contain device_id")
        elif self.device_id is None:
            raise ValueError("Device Principal requires device_id")
        else:
            require_id(self.device_id, "device_id")


class DemoIdentityProvider:
    def __init__(
        self,
        *,
        organization_id: str,
        user_id: str,
        device_id: str,
        device_token_sha256: str,
    ) -> None:
        self.organization_id = require_id(organization_id, "organization_id")
        self.user_id = require_id(user_id, "user_id")
        self.device_id = require_id(device_id, "device_id")
        self.device_token_sha256 = require_sha256(
            device_token_sha256, "device_token_sha256"
        )

    def browser_principal(self) -> Principal:
        return Principal("user", self.organization_id, self.user_id, None)

    def authenticate_device(self, authorization: str | None) -> Principal:
        if (
            not isinstance(authorization, str)
            or not authorization.startswith("Bearer ")
        ):
            raise InvalidCredentials()
        token = authorization.removeprefix("Bearer ")
        if not token or any(character.isspace() for character in token):
            raise InvalidCredentials()
        supplied = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(supplied, self.device_token_sha256):
            raise InvalidCredentials()
        return Principal(
            "device",
            self.organization_id,
            self.device_id,
            self.device_id,
        )


def require_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value

