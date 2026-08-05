from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from zdecision.agent.capture_routing import CaptureRoutingStore, plan_capture_group
from zdecision.agent.git_path_evidence import FrozenGitPathEvidence
from zdecision.agent.repository_routes import RepositoryRouteSnapshot
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.central.decision_spaces import RepositoryDecisionRoute
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import ClaimedCaptureGroup


REQUEST_ID = "crq_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
CLOUD_SPACE_ID = "dsp_" + "3" * 32
ZCF_LICENSE_SPACE_ID = "dsp_" + "4" * 32
THEME_SPACE_ID = "dsp_" + "5" * 32


class CaptureRoutingTest(unittest.TestCase):
    def route(
        self,
        digit: str,
        prefix: str,
        decision_space_id: str,
        *,
        excluded: tuple[str, ...] = (),
    ) -> RepositoryDecisionRoute:
        return RepositoryDecisionRoute(
            route_id="drr_" + digit * 32,
            repository_id=REPOSITORY_ID,
            decision_space_id=decision_space_id,
            path_prefixes=(prefix,),
            excluded_prefixes=excluded,
            enabled=True,
            configuration_version=1,
        )

    def route_snapshot(
        self,
        routes: tuple[RepositoryDecisionRoute, ...] | None = None,
    ) -> RepositoryRouteSnapshot:
        return RepositoryRouteSnapshot.create(
            REPOSITORY_ID,
            routes
            or (
                self.route(
                    "a", "packages/products/cloud", CLOUD_SPACE_ID
                ),
                self.route(
                    "b",
                    "packages/products/shared/zcf-license",
                    ZCF_LICENSE_SPACE_ID,
                ),
                self.route("c", "packages/shared/theme", THEME_SPACE_ID),
            ),
        )

    def claimed_group(
        self, snapshot: RepositoryRouteSnapshot
    ) -> ClaimedCaptureGroup:
        return ClaimedCaptureGroup(
            request_id=REQUEST_ID,
            repository_id=REPOSITORY_ID,
            template_id="business",
            capture_scope="all_valid_sessions",
            client_action_id="web_action_task_3",
            route_snapshot=snapshot.routes,
            route_snapshot_digest=snapshot.digest,
            lease_token="lease_0123456789abcdef",
            lease_expires_at="2026-08-05T05:00:30Z",
        )

    def evidence(self, *paths: str) -> FrozenGitPathEvidence:
        ordered = tuple(sorted(paths))
        return FrozenGitPathEvidence.create(
            repository_id=REPOSITORY_ID,
            head_commit="d" * 40,
            commit_ranges=(),
            paths=ordered,
        )

    def sources(self) -> tuple[FrozenSessionSource, ...]:
        return (
            FrozenSessionSource(
                request_id=REQUEST_ID,
                source_key="src_" + "6" * 32,
                repository_id=REPOSITORY_ID,
                session_id="019fb100-0000-7000-8000-000000000001",
                cwd="/private/repository",
                lineage="lin_" + "7" * 32,
                previous_handled_turn_id=None,
                upper_turn_id="019fb100-0000-7000-8000-000000000002",
                source_fingerprint="8" * 64,
                previous_handled_head_commit=None,
                upper_head_commit="d" * 40,
            ),
        )

    def test_cloud_license_and_theme_paths_make_three_leaf_slices(self) -> None:
        snapshot = self.route_snapshot()
        plan = plan_capture_group(
            self.claimed_group(snapshot),
            snapshot,
            self.evidence(
                "packages/products/cloud/apps/core-shell/src/app.tsx",
                "packages/products/shared/zcf-license/src/App.tsx",
                "packages/shared/theme/src/index.ts",
            ),
            self.sources(),
        )

        self.assertEqual(
            (CLOUD_SPACE_ID, ZCF_LICENSE_SPACE_ID, THEME_SPACE_ID),
            tuple(item.decision_space_id for item in plan.slices),
        )

    def test_shared_leaf_does_not_fan_out_to_consuming_products(self) -> None:
        snapshot = self.route_snapshot(
            (
                self.route(
                    "a",
                    ".",
                    CLOUD_SPACE_ID,
                    excluded=("packages/products/shared",),
                ),
                self.route(
                    "b",
                    "packages/products/shared/zcf-license",
                    ZCF_LICENSE_SPACE_ID,
                ),
            )
        )
        plan = plan_capture_group(
            self.claimed_group(snapshot),
            snapshot,
            self.evidence(
                "packages/products/shared/zcf-license/src/App.tsx"
            ),
            self.sources(),
        )

        self.assertEqual(
            (ZCF_LICENSE_SPACE_ID,),
            tuple(item.decision_space_id for item in plan.slices),
        )

    def test_ambiguous_path_fails_closed(self) -> None:
        snapshot = self.route_snapshot(
            (
                self.route("a", "packages/shared", CLOUD_SPACE_ID),
                self.route(
                    "b", "packages/shared/theme", THEME_SPACE_ID
                ),
            )
        )

        with self.assertRaisesRegex(
            ValueError, "decision_space_route_ambiguous"
        ):
            plan_capture_group(
                self.claimed_group(snapshot),
                snapshot,
                self.evidence("packages/shared/theme/src/index.ts"),
                self.sources(),
            )

    def test_zero_matches_returns_empty_plan_and_stable_boundary(self) -> None:
        snapshot = self.route_snapshot()

        plan = plan_capture_group(
            self.claimed_group(snapshot),
            snapshot,
            self.evidence("docs/architecture.md"),
            self.sources(),
        )

        self.assertEqual((), plan.slices)
        self.assertEqual((), plan.route_selections())
        self.assertEqual(
            hashlib.sha256(
                canonical_json_bytes(
                    {
                        "sources": [
                            {
                                "source_key": "src_" + "6" * 32,
                                "source_fingerprint": "8" * 64,
                                "previous_handled_head_commit": None,
                                "upper_head_commit": "d" * 40,
                            }
                        ]
                    }
                )
            ).hexdigest(),
            plan.source_boundary_digest,
        )

    def test_route_snapshot_digest_mismatch_is_terminal_to_planning(self) -> None:
        snapshot = self.route_snapshot()
        different = RepositoryRouteSnapshot(
            snapshot.repository_id,
            snapshot.routes,
            "f" * 64,
        )

        with self.assertRaisesRegex(ValueError, "route_snapshot_mismatch"):
            plan_capture_group(
                self.claimed_group(snapshot),
                different,
                self.evidence("packages/shared/theme/src/index.ts"),
                self.sources(),
            )

    def test_persisted_plan_replay_does_not_rebind_changed_paths(self) -> None:
        snapshot = self.route_snapshot()
        group = self.claimed_group(snapshot)
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureRoutingStore.open(
                Path(directory) / "routing.sqlite3"
            )
            self.addCleanup(store.close)
            first = store.get_or_create_plan(
                group,
                snapshot,
                self.sources(),
                self.evidence("packages/shared/theme/src/index.ts"),
            )
            replay = store.get_or_create_plan(
                group,
                snapshot,
                self.sources(),
                self.evidence(
                    "packages/products/cloud/apps/core-shell/src/app.tsx"
                ),
            )

        self.assertEqual(first, replay)
        self.assertEqual((THEME_SPACE_ID,), tuple(
            item.decision_space_id for item in replay.slices
        ))


if __name__ == "__main__":
    unittest.main()
