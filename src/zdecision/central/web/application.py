"""Application boundary for the Central Decision Web."""

from __future__ import annotations

from zdecision.central.auth import Principal
from zdecision.central.web.queries import CentralWebQueries, DashboardView
from zdecision.central.web.store import CentralWebStore


class CentralWebApplication:
    def __init__(
        self, *, store: CentralWebStore, queries: CentralWebQueries
    ) -> None:
        if not isinstance(store, CentralWebStore):
            raise TypeError("store must be a CentralWebStore")
        if not isinstance(queries, CentralWebQueries):
            raise TypeError("queries must be CentralWebQueries")
        self.store = store
        self.queries = queries

    def dashboard(self, principal: Principal) -> DashboardView:
        return self.queries.dashboard(principal)
