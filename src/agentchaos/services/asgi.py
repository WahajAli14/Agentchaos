"""Standalone entry points, one process per service (used by Docker Compose).

    uvicorn agentchaos.services.asgi:orders --factory

Each process has its own logical clock, driven through `/_admin/clock/advance`.
The database is reset to the fixture at startup, so seeded IDs never collide
with rows left over from a previous process.
"""

from fastapi import FastAPI

from agentchaos.config import get_settings
from agentchaos.services.base import Runtime
from agentchaos.services.cluster import SERVICE_CLASSES


def _standalone(name: str) -> FastAPI:
    settings = get_settings()
    service = SERVICE_CLASSES[name](
        settings.services_dir / f"{name}.db", Runtime.create(settings.default_seed)
    )
    service.reset()
    return service.app


def orders() -> FastAPI:
    return _standalone("orders")


def inventory() -> FastAPI:
    return _standalone("inventory")


def billing() -> FastAPI:
    return _standalone("billing")


def notifications() -> FastAPI:
    return _standalone("notifications")
