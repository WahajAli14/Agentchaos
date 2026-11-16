"""Orders service: create, confirm, cancel."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, Connection, Integer, MetaData, String, Table, insert, select, update

from agentchaos.services.base import MockService, Rejected, Unchanged


class CreateOrder(BaseModel):
    customer_id: str
    sku: str
    quantity: int = Field(gt=0)
    unit_price_cents: int = Field(ge=0)


class ConfirmOrder(BaseModel):
    reservation_id: str | None = None
    invoice_id: str | None = None


class OrdersService(MockService):
    name = "orders"

    def define_tables(self, metadata: MetaData) -> None:
        self.orders = Table(
            "orders",
            metadata,
            Column("id", String, primary_key=True),
            Column("customer_id", String, nullable=False),
            Column("sku", String, nullable=False),
            Column("quantity", Integer, nullable=False),
            Column("unit_price_cents", Integer, nullable=False),
            Column("status", String, nullable=False),  # created | confirmed | cancelled
            Column("reservation_id", String, nullable=True),
            Column("invoice_id", String, nullable=True),
            Column("created_at_ms", Integer, nullable=False),
            Column("confirmed_at_ms", Integer, nullable=True),
            Column("cancelled_at_ms", Integer, nullable=True),
        )

    def _get(self, conn: Connection, order_id: str) -> dict[str, Any]:
        row = conn.execute(select(self.orders).where(self.orders.c.id == order_id)).first()
        if row is None:
            raise Rejected(404, {"error": "order_not_found", "order_id": order_id})
        return dict(row._mapping)

    def define_routes(self, app: FastAPI) -> None:
        orders = self.orders

        @app.post("/orders")
        async def create_order(body: CreateOrder, request: Request) -> JSONResponse:
            order_id = self.runtime.ids.new("ord")

            def commit(conn: Connection) -> tuple[int, dict[str, Any]]:
                conn.execute(
                    insert(orders).values(
                        id=order_id,
                        status="created",
                        created_at_ms=self.runtime.clock.now_ms(),
                        **body.model_dump(),
                    )
                )
                return 201, self._get(conn, order_id)

            # Order creation is always synchronous: later steps need the order to exist.
            return self.mutate(request, "create_order", body.model_dump(), commit, deferrable=False)

        @app.get("/orders/{order_id}")
        async def get_order(order_id: str) -> JSONResponse:
            with self.engine.connect() as conn:
                try:
                    return JSONResponse(self._get(conn, order_id))
                except Rejected as rejected:
                    return JSONResponse(rejected.body, rejected.status)

        @app.get("/orders")
        async def list_orders(customer_id: str | None = None) -> list[dict[str, Any]]:
            query = select(orders).order_by(orders.c.id)
            if customer_id is not None:
                query = query.where(orders.c.customer_id == customer_id)
            with self.engine.connect() as conn:
                return [dict(row._mapping) for row in conn.execute(query)]

        @app.post("/orders/{order_id}/confirm")
        async def confirm_order(
            order_id: str, body: ConfirmOrder, request: Request
        ) -> JSONResponse:
            # The orders service trusts the caller: it does not check that the
            # invoice or reservation exist in the other services.
            def commit(conn: Connection) -> tuple[int, dict[str, Any]]:
                order = self._get(conn, order_id)
                if order["status"] == "cancelled":
                    raise Rejected(409, {"error": "order_cancelled", "order_id": order_id})
                if order["status"] == "confirmed":
                    raise Unchanged(200, order)
                conn.execute(
                    update(orders)
                    .where(orders.c.id == order_id)
                    .values(
                        status="confirmed",
                        confirmed_at_ms=self.runtime.clock.now_ms(),
                        reservation_id=body.reservation_id,
                        invoice_id=body.invoice_id,
                    )
                )
                return 200, self._get(conn, order_id)

            payload = {"order_id": order_id, **body.model_dump()}
            return self.mutate(
                request, "confirm_order", payload, commit, pending_body={"id": order_id}
            )

        @app.post("/orders/{order_id}/cancel")
        async def cancel_order(order_id: str, request: Request) -> JSONResponse:
            def commit(conn: Connection) -> tuple[int, dict[str, Any]]:
                order = self._get(conn, order_id)
                if order["status"] == "confirmed":
                    raise Rejected(409, {"error": "order_already_confirmed", "order_id": order_id})
                if order["status"] == "cancelled":
                    raise Unchanged(200, order)
                conn.execute(
                    update(orders)
                    .where(orders.c.id == order_id)
                    .values(status="cancelled", cancelled_at_ms=self.runtime.clock.now_ms())
                )
                return 200, self._get(conn, order_id)

            return self.mutate(
                request,
                "cancel_order",
                {"order_id": order_id},
                commit,
                pending_body={"id": order_id},
            )
