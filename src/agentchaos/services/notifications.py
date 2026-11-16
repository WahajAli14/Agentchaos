"""Notifications service: records messages sent to the customer."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Column, Connection, Integer, MetaData, String, Table, insert, select

from agentchaos.services.base import MockService, Rejected


class CreateNotification(BaseModel):
    order_id: str
    kind: str = "order_confirmed"
    message: str = ""


class NotificationsService(MockService):
    name = "notifications"

    def define_tables(self, metadata: MetaData) -> None:
        self.notifications = Table(
            "notifications",
            metadata,
            Column("id", String, primary_key=True),
            Column("order_id", String, nullable=False),
            Column("kind", String, nullable=False),
            Column("message", String, nullable=False),
            Column("created_at_ms", Integer, nullable=False),
        )

    def define_routes(self, app: FastAPI) -> None:
        notifications = self.notifications

        @app.post("/notifications")
        async def create_notification(body: CreateNotification, request: Request) -> JSONResponse:
            notification_id = self.runtime.ids.new("ntf")

            def commit(conn: Connection) -> tuple[int, dict[str, Any]]:
                if not self.flaws.accept_duplicates:
                    existing = conn.execute(
                        select(notifications.c.id).where(
                            notifications.c.order_id == body.order_id,
                            notifications.c.kind == body.kind,
                        )
                    ).first()
                    if existing is not None:
                        raise Rejected(
                            409, {"error": "duplicate_notification", "existing_id": existing.id}
                        )
                row = {
                    "id": notification_id,
                    "created_at_ms": self.runtime.clock.now_ms(),
                    **body.model_dump(),
                }
                conn.execute(insert(notifications).values(**row))
                return 201, row

            return self.mutate(
                request,
                "create_notification",
                body.model_dump(),
                commit,
                pending_body={"id": notification_id},
            )

        @app.get("/notifications")
        async def list_notifications(order_id: str | None = None) -> list[dict[str, Any]]:
            query = select(notifications).order_by(notifications.c.id)
            if order_id is not None:
                query = query.where(notifications.c.order_id == order_id)
            with self.engine.connect() as conn:
                return [dict(row._mapping) for row in conn.execute(query)]
