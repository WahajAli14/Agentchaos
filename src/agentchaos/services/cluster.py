"""All four mock services wired to one deterministic runtime, in one process."""

from collections.abc import Mapping
from pathlib import Path

import httpx

from agentchaos.invariants.base import GroundTruth
from agentchaos.services.base import MockService, Runtime, ServiceFlaws, Snapshot
from agentchaos.services.billing import BillingService
from agentchaos.services.fixtures import DEFAULT_FIXTURE, Fixture
from agentchaos.services.inventory import InventoryService
from agentchaos.services.notifications import NotificationsService
from agentchaos.services.orders import OrdersService

SERVICE_CLASSES: dict[str, type[MockService]] = {
    "orders": OrdersService,
    "inventory": InventoryService,
    "billing": BillingService,
    "notifications": NotificationsService,
}


class ServiceCluster:
    def __init__(
        self,
        services_dir: Path,
        seed: int,
        flaws: Mapping[str, ServiceFlaws] | None = None,
    ) -> None:
        self.services_dir = services_dir
        self.runtime = Runtime.create(seed)
        self.services: dict[str, MockService] = {
            name: cls(services_dir / f"{name}.db", self.runtime, (flaws or {}).get(name))
            for name, cls in SERVICE_CLASSES.items()
        }

    def reset(self, seed: int, fixture: Fixture = DEFAULT_FIXTURE) -> None:
        """Return clock, scheduler, IDs and every mock service DB to a known state.

        Only touches the mock service databases. The results database is a
        separate store this class has no handle on.
        """
        self.runtime.reset(seed)
        for service in self.services.values():
            service.reset(fixture)

    def client(self) -> httpx.AsyncClient:
        """HTTP client that routes `http://<service>/...` to the in-process apps."""
        mounts: dict[str, httpx.AsyncBaseTransport | None] = {
            f"http://{name}": httpx.ASGITransport(app=service.app)
            for name, service in self.services.items()
        }
        return httpx.AsyncClient(mounts=mounts)

    def snapshot(self) -> dict[str, Snapshot]:
        return {name: service.snapshot() for name, service in self.services.items()}

    def ground_truth(self) -> GroundTruth:
        snap = self.snapshot()
        return GroundTruth(
            orders=snap["orders"]["orders"],
            stock=snap["inventory"]["stock"],
            reservations=snap["inventory"]["reservations"],
            invoices=snap["billing"]["invoices"],
            notifications=snap["notifications"]["notifications"],
            pending_async_operations=len(self.runtime.scheduler.pending()),
        )

    def close(self) -> None:
        for service in self.services.values():
            service.close()
