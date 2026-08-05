"""Immutable local copy of one server-frozen repository route snapshot."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from zdecision.central.decision_spaces import RepositoryDecisionRoute
from zdecision.jsonio import canonical_json_bytes


_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RepositoryRouteSnapshot:
    repository_id: str
    routes: tuple[RepositoryDecisionRoute, ...]
    digest: str

    def __post_init__(self) -> None:
        if _REPOSITORY_ID.fullmatch(self.repository_id) is None:
            raise ValueError("repository_id is invalid")
        if (
            not isinstance(self.routes, tuple)
            or any(
                not isinstance(route, RepositoryDecisionRoute)
                for route in self.routes
            )
        ):
            raise ValueError("routes are invalid")
        if _DIGEST.fullmatch(self.digest) is None:
            raise ValueError("digest is invalid")

    @classmethod
    def create(
        cls,
        repository_id: str,
        routes: tuple[RepositoryDecisionRoute, ...],
    ) -> "RepositoryRouteSnapshot":
        if _REPOSITORY_ID.fullmatch(repository_id) is None:
            raise ValueError("repository_id is invalid")
        if (
            not isinstance(routes, tuple)
            or any(
                not isinstance(route, RepositoryDecisionRoute)
                for route in routes
            )
        ):
            raise ValueError("routes are invalid")
        if any(route.repository_id != repository_id for route in routes):
            raise ValueError("route_repository_mismatch")
        if any(route.decision_space_id.startswith("dsg_") for route in routes):
            raise ValueError("generic_shared_route_forbidden")
        identities = tuple(
            (route.route_id, route.configuration_version) for route in routes
        )
        if len(set(identities)) != len(identities):
            raise ValueError("route_identity_duplicate")
        ordered = tuple(
            sorted(
                routes,
                key=lambda item: (
                    item.route_id,
                    item.configuration_version,
                ),
            )
        )
        digest = hashlib.sha256(
            canonical_json_bytes(
                {"routes": [route.to_dict() for route in ordered]}
            )
        ).hexdigest()
        return cls(repository_id, ordered, digest)

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "routes": [route.to_dict() for route in self.routes],
            "digest": self.digest,
        }

