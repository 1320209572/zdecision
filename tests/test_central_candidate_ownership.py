from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from zdecision.capture.models import CandidateContent
from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import (
    CatalogGroup,
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.service import CaptureRequestService, RequestConflict
from zdecision.central.store import CentralStore
from zdecision.ids import (
    candidate_family_id,
    candidate_revision_id,
    catalog_group_id,
    decision_space_id,
    product_id,
    repository_route_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateRevisionUpload,
    CandidateSliceBatchUpload,
    CaptureGroupCreate,
    RouteSelection,
)


NOW = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
REPOSITORY_ID = "repo_" + "1" * 32


class CentralCandidateOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = CentralStore.open(
            Path(self.temporary_directory.name) / "central.sqlite3"
        )
        self.service = CaptureRequestService(self.store)
        self.user = Principal("user", "org_demo", "user_demo", None)
        self.device = Principal(
            "device", "org_demo", "device_demo", "device_demo"
        )
        self.store.put_repository(
            "org_demo", EnabledRepository(REPOSITORY_ID, True)
        )
        shared = CatalogGroup(
            catalog_group_id=catalog_group_id(("Shared",)),
            parent_group_id=None,
            display_name="Shared",
            breadcrumb=("Shared",),
            source_prefix=None,
            sort_order=10,
        )
        self.store.put_catalog_group("org_demo", shared)
        self.cloud = self.space("Cloud", "product", "packages/products/cloud")
        self.license = self.space(
            "zcf-license", "shared_unit", "packages/products/shared/zcf-license"
        )
        self.theme = self.space(
            "theme", "shared_unit", "packages/shared/theme"
        )
        for space in (self.cloud, self.license, self.theme):
            self.store.put_decision_space("org_demo", space)
        self.routes = tuple(
            self.route(space) for space in (self.cloud, self.license, self.theme)
        )
        self.store.replace_trusted_route_heads(
            "org_demo", REPOSITORY_ID, self.routes
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def space(
        self, name: str, kind: str, source_root: str
    ) -> LeafDecisionSpace:
        compatibility_name = name if kind == "product" else f"Shared / {source_root}"
        compatibility_id = product_id(compatibility_name)
        return LeafDecisionSpace(
            decision_space_id=decision_space_id(kind, compatibility_id),
            kind=kind,
            display_name=name,
            compatibility_product_id=compatibility_id,
            compatibility_product_name=compatibility_name,
            catalog_group_id=(
                None if kind == "product" else catalog_group_id(("Shared",))
            ),
            catalog_breadcrumb=() if kind == "product" else ("Shared",),
            source_root=source_root,
            package_name=None if kind == "product" else f"@zstack/{name}",
            asset_type=None if kind == "product" else "library",
            enabled=True,
        )

    def route(
        self, space: LeafDecisionSpace, version: int = 1
    ) -> RepositoryDecisionRoute:
        return RepositoryDecisionRoute(
            route_id=repository_route_id(REPOSITORY_ID, space.decision_space_id),
            repository_id=REPOSITORY_ID,
            decision_space_id=space.decision_space_id,
            path_prefixes=(space.source_root,),
            excluded_prefixes=(),
            enabled=True,
            configuration_version=version,
        )

    def command(self, action: str = "web_action_task_2") -> CaptureGroupCreate:
        return CaptureGroupCreate(
            repository_id=REPOSITORY_ID,
            template_id="business",
            capture_scope="all_valid_sessions",
            client_action_id=action,
        )

    def selection(self, route: RepositoryDecisionRoute, digit: str) -> RouteSelection:
        return RouteSelection(
            route_id=route.route_id,
            configuration_version=route.configuration_version,
            matched_path_digest=digit * 64,
            source_boundary_digest=(str((int(digit) + 3) % 10)) * 64,
        )

    def plan_theme_slice(self):
        group = self.service.create_group(self.user, self.command(), NOW)
        claimed = self.service.claim_next_group(self.device, NOW)
        self.assertIsNotNone(claimed)
        slices = self.service.plan_slices(
            self.device,
            group.request_id,
            claimed.lease_token,
            (self.selection(self.routes[2], "3"),),
            NOW,
        )
        return slices[0], claimed.lease_token

    def batch(self, slice_view, *, decision_space_id: str | None = None):
        content = CandidateContent(
            product=slice_view.ownership.compatibility_product_name,
            claim="Frozen ownership survives later route changes.",
            future_action="Review this Candidate under its captured leaf.",
            scope_summary="Task 2 ownership boundary",
            repositories=(REPOSITORY_ID,),
            paths=(),
            invalidation_conditions=("The captured behavior changes.",),
        )
        content_digest = hashlib.sha256(
            canonical_json_bytes(content.to_dict())
        ).hexdigest()
        family_id = candidate_family_id(
            REPOSITORY_ID, "cand_" + "5" * 32 + "_01"
        )
        item = CandidateRevisionUpload(
            family_id=family_id,
            revision_id=candidate_revision_id(family_id, 1, content_digest),
            revision=1,
            content=content,
            content_digest=content_digest,
            evidence_digest="e" * 64,
        )
        return CandidateSliceBatchUpload(
            request_id=slice_view.request_id,
            slice_id=slice_view.slice_id,
            route_id=slice_view.ownership.route_id,
            route_configuration_version=(
                slice_view.ownership.route_configuration_version
            ),
            decision_space_id=(
                decision_space_id or slice_view.ownership.decision_space_id
            ),
            items=(item,),
            batch_digest=hashlib.sha256(
                canonical_json_bytes({"items": [item.to_dict()]})
            ).hexdigest(),
        )

    def test_one_action_plans_three_frozen_leaf_slices(self) -> None:
        group = self.service.create_group(self.user, self.command(), NOW)
        claimed = self.service.claim_next_group(self.device, NOW)
        self.assertIsNotNone(claimed)
        slices = self.service.plan_slices(
            self.device,
            claimed.request_id,
            claimed.lease_token,
            tuple(
                self.selection(route, str(index))
                for index, route in enumerate(self.routes, start=1)
            ),
            NOW,
        )
        self.assertEqual(group.request_id, claimed.request_id)
        self.assertEqual(3, len(slices))
        self.assertEqual(
            {
                self.cloud.decision_space_id,
                self.license.decision_space_id,
                self.theme.decision_space_id,
            },
            {item.ownership.decision_space_id for item in slices},
        )
        replay = self.service.plan_slices(
            self.device,
            claimed.request_id,
            claimed.lease_token,
            tuple(
                self.selection(route, str(index))
                for index, route in enumerate(self.routes, start=1)
            ),
            NOW,
        )
        self.assertEqual(slices, replay)

    def test_upload_cannot_override_frozen_leaf(self) -> None:
        slice_view, lease_token = self.plan_theme_slice()
        batch = self.batch(
            slice_view, decision_space_id=self.cloud.decision_space_id
        )
        with self.assertRaisesRegex(RequestConflict, "slice_ownership_conflict"):
            self.service.accept_slice_batch(
                self.device,
                slice_view.request_id,
                slice_view.slice_id,
                lease_token,
                batch,
                NOW,
            )

    def test_new_route_head_does_not_move_existing_candidate(self) -> None:
        group = self.service.create_group(self.user, self.command(), NOW)
        claimed = self.service.claim_next_group(self.device, NOW)
        slices = self.service.plan_slices(
            self.device,
            group.request_id,
            claimed.lease_token,
            (self.selection(self.routes[2], "3"),),
            NOW,
        )
        batch = self.batch(slices[0])
        self.service.accept_slice_batch(
            self.device,
            group.request_id,
            slices[0].slice_id,
            claimed.lease_token,
            batch,
            NOW,
        )
        self.store.put_route_version(
            "org_demo", replace(self.routes[2], configuration_version=2)
        )

        ownership = self.store.candidate_ownership(
            "org_demo", REPOSITORY_ID, batch.items[0].family_id, 1
        )

        self.assertEqual(self.theme.decision_space_id, ownership.decision_space_id)
        self.assertEqual(1, ownership.route_configuration_version)
        self.assertEqual(
            self.theme.decision_space_id,
            self.store.connection.execute(
                """SELECT decision_space_id FROM candidate_family_heads
                WHERE organization_id = 'org_demo' AND repository_id = ?
                AND family_id = ?""",
                (REPOSITORY_ID, batch.items[0].family_id),
            ).fetchone()["decision_space_id"],
        )

    def test_empty_route_selection_is_a_successful_terminal_result(self) -> None:
        group = self.service.create_group(self.user, self.command(), NOW)
        claimed = self.service.claim_next_group(self.device, NOW)

        self.assertEqual(
            (),
            self.service.plan_slices(
                self.device,
                group.request_id,
                claimed.lease_token,
                (),
                NOW,
            ),
        )
        completed = self.service.get_group(self.user, group.request_id)
        self.assertEqual("succeeded_no_candidates", completed.state)


if __name__ == "__main__":
    unittest.main()
