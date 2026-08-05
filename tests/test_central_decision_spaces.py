from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import (
    CatalogGroup,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.ids import (
    catalog_group_id,
    decision_space_id,
    product_id,
    repository_route_id,
)
from zdecision.sync.contracts import EnabledRepository


REPOSITORY_ID = "repo_" + "1" * 32
SINGLE_PRODUCT_REPOSITORY_ID = "repo_" + "2" * 32


class CentralDecisionSpacesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = CentralStore.open(
            Path(self.temporary_directory.name) / "central.sqlite3"
        )
        self.service = CaptureRequestService(self.store)
        self.user = Principal("user", "org_demo", "user_demo", None)
        self.store.put_repository("org_demo", EnabledRepository(REPOSITORY_ID, True))
        self.store.put_repository(
            "org_demo", EnabledRepository(SINGLE_PRODUCT_REPOSITORY_ID, True)
        )
        self.cloud = self.product_space("Cloud", "packages/products/cloud")
        self.audit = self.shared_space(
            "zcf-audit", "packages/products/shared/zcf-audit", "@zstack/zcf-audit"
        )
        self.theme = self.shared_space(
            "theme", "packages/shared/theme", "@zstack/theme"
        )
        self.design = self.shared_space(
            "design", "packages/design", "@zstack/design"
        )
        for space in (self.cloud, self.audit, self.theme, self.design):
            self.store.put_decision_space("org_demo", space)
        self.store.replace_trusted_route_heads(
            "org_demo",
            REPOSITORY_ID,
            (
                self.route(self.cloud, "packages/products/cloud"),
                self.route(self.audit, "packages/products/shared/zcf-audit"),
                self.route(self.theme, "packages/shared/theme"),
                self.route(self.design, "packages/design"),
            ),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def product_space(self, name: str, source_root: str) -> LeafDecisionSpace:
        compatibility_id = product_id(name)
        return LeafDecisionSpace(
            decision_space_id=decision_space_id("product", compatibility_id),
            kind="product",
            display_name=name,
            compatibility_product_id=compatibility_id,
            compatibility_product_name=name,
            catalog_group_id=None,
            catalog_breadcrumb=(),
            source_root=source_root,
            package_name=None,
            asset_type=None,
            enabled=True,
        )

    def shared_space(
        self, name: str, source_root: str, package_name: str
    ) -> LeafDecisionSpace:
        shared = CatalogGroup(
            catalog_group_id=catalog_group_id(("Shared",)),
            parent_group_id=None,
            display_name="Shared",
            breadcrumb=("Shared",),
            source_prefix=None,
            sort_order=20,
        )
        self.store.put_catalog_group("org_demo", shared)
        compatibility_id = product_id(f"Shared / {source_root}")
        return LeafDecisionSpace(
            decision_space_id=decision_space_id("shared_unit", compatibility_id),
            kind="shared_unit",
            display_name=name,
            compatibility_product_id=compatibility_id,
            compatibility_product_name=f"Shared / {source_root}",
            catalog_group_id=shared.catalog_group_id,
            catalog_breadcrumb=("Shared",),
            source_root=source_root,
            package_name=package_name,
            asset_type="library",
            enabled=True,
        )

    def route(
        self, space: LeafDecisionSpace, path_prefix: str, version: int = 1
    ) -> RepositoryDecisionRoute:
        return RepositoryDecisionRoute(
            route_id=repository_route_id(REPOSITORY_ID, space.decision_space_id),
            repository_id=REPOSITORY_ID,
            decision_space_id=space.decision_space_id,
            path_prefixes=(path_prefix,),
            excluded_prefixes=(),
            enabled=True,
            configuration_version=version,
        )

    def test_shared_group_cannot_be_a_route_target(self) -> None:
        shared = CatalogGroup(
            catalog_group_id="dsg_" + "1" * 32,
            parent_group_id=None,
            display_name="Shared routes",
            breadcrumb=("Shared routes",),
            source_prefix=None,
            sort_order=30,
        )
        self.store.put_catalog_group("org_demo", shared)
        with self.assertRaisesRegex(ValueError, "route_target_must_be_leaf"):
            self.store.put_route_version(
                "org_demo",
                RepositoryDecisionRoute(
                    route_id="drr_" + "2" * 32,
                    repository_id=REPOSITORY_ID,
                    decision_space_id=shared.catalog_group_id,
                    path_prefixes=("packages/products/shared",),
                    excluded_prefixes=(),
                    enabled=True,
                    configuration_version=1,
                ),
            )

    def test_one_repository_returns_product_and_shared_leaf_routes(self) -> None:
        routes = self.service.list_repository_spaces(self.user, REPOSITORY_ID)
        self.assertEqual(
            {"Cloud", "zcf-audit", "theme", "design"},
            {item.display_name for item in routes.spaces},
        )
        self.assertEqual("Shared", routes.shared_tree.display_name)

    def test_route_version_append_does_not_overwrite_v1(self) -> None:
        first = self.route(self.theme, "packages/shared/theme", version=1)
        second = self.route(self.theme, "packages/shared/theme-v2", version=2)
        self.store.put_route_version("org_demo", first)
        self.store.put_route_version("org_demo", second)
        self.assertEqual(
            (1, 2),
            tuple(
                item.configuration_version
                for item in self.store.route_history("org_demo", first.route_id)
            ),
        )

    def test_true_single_product_repository_accepts_one_root_route(self) -> None:
        product = self.product_space("Standalone", ".")
        self.store.put_decision_space("org_demo", product)
        route = RepositoryDecisionRoute(
            route_id=repository_route_id(
                SINGLE_PRODUCT_REPOSITORY_ID, product.decision_space_id
            ),
            repository_id=SINGLE_PRODUCT_REPOSITORY_ID,
            decision_space_id=product.decision_space_id,
            path_prefixes=(".",),
            excluded_prefixes=(),
            enabled=True,
            configuration_version=1,
        )
        self.store.replace_trusted_route_heads(
            "org_demo", SINGLE_PRODUCT_REPOSITORY_ID, (route,)
        )
        self.assertTrue(route.matches("src/main.py"))


if __name__ == "__main__":
    unittest.main()
