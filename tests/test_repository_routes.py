from __future__ import annotations

import hashlib
import unittest

from zdecision.agent.repository_routes import RepositoryRouteSnapshot
from zdecision.central.decision_spaces import RepositoryDecisionRoute
from zdecision.jsonio import canonical_json_bytes


REPOSITORY_ID = "repo_" + "1" * 32
OTHER_REPOSITORY_ID = "repo_" + "2" * 32
SHARED_GROUP_ID = "dsg_" + "3" * 32


def route(
    digit: str,
    prefix: str,
    decision_space_id: str | None = None,
    *,
    repository_id: str = REPOSITORY_ID,
) -> RepositoryDecisionRoute:
    return RepositoryDecisionRoute(
        route_id="drr_" + digit * 32,
        repository_id=repository_id,
        decision_space_id=decision_space_id or "dsp_" + digit * 32,
        path_prefixes=(prefix,),
        excluded_prefixes=(),
        enabled=True,
        configuration_version=1,
    )


class RepositoryRouteSnapshotTest(unittest.TestCase):
    def test_snapshot_uses_exact_sorted_task_2_wire_projection(self) -> None:
        second = route("b", "packages/shared/theme")
        first = route("a", "packages/products/cloud")

        snapshot = RepositoryRouteSnapshot.create(
            REPOSITORY_ID, (second, first)
        )

        self.assertEqual((first, second), snapshot.routes)
        self.assertEqual(
            hashlib.sha256(
                canonical_json_bytes(
                    {"routes": [first.to_dict(), second.to_dict()]}
                )
            ).hexdigest(),
            snapshot.digest,
        )

    def test_route_from_another_repository_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "route_repository_mismatch"
        ):
            RepositoryRouteSnapshot.create(
                REPOSITORY_ID,
                (
                    route(
                        "a",
                        "packages/products/cloud",
                        repository_id=OTHER_REPOSITORY_ID,
                    ),
                ),
            )

    def test_generic_shared_route_is_rejected_before_matching(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "generic_shared_route_forbidden"
        ):
            RepositoryRouteSnapshot.create(
                REPOSITORY_ID,
                (
                    route(
                        "a",
                        "packages/products/shared",
                        SHARED_GROUP_ID,
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
