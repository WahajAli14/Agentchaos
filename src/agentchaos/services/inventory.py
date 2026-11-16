"""Inventory service: stock levels, reserve, release."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, Connection, Integer, MetaData, String, Table, insert, select, update

from agentchaos.services.base import MockService, Rejected
from agentchaos.services.fixtures import Fixture


class CreateReservation(BaseModel):
    order_id: str
    sku: str
    quantity: int = Field(gt=0)


class InventoryService(MockService):
    name = "inventory"

    def define_tables(self, metadata: MetaData) -> None:
        self.stock = Table(
            "stock",
            metadata,
            Column("sku", String, primary_key=True),
            Column("on_hand", Integer, nullable=False),
            Column("reserved", Integer, nullable=False),
        )
        self.reservations = Table(
            "reservations",
            metadata,
            Column("id", String, primary_key=True),
            Column("order_id", String, nullable=False),
            Column("sku", String, nullable=False),
            Column("quantity", Integer, nullable=False),
            Column("status", String, nullable=False),  # active | released
            Column("created_at_ms", Integer, nullable=False),
            Column("released_at_ms", Integer, nullable=True),
        )

    def seed(self, conn: Connection, fixture: Fixture) -> None:
        for sku, on_hand in sorted(fixture.stock.items()):
            conn.execute(insert(self.stock).values(sku=sku, on_hand=on_hand, reserved=0))

    def _reservation(self, conn: Connection, reservation_id: str) -> dict[str, Any]:
        row = conn.execute(
            select(self.reservations).where(self.reservations.c.id == reservation_id)
        ).first()
        if row is None:
            raise Rejected(404, {"error": "reservation_not_found", "id": reservation_id})
        return dict(row._mapping)

    def define_routes(self, app: FastAPI) -> None:
        stock, reservations = self.stock, self.reservations

        @app.get("/stock/{sku}")
        async def get_stock(sku: str) -> JSONResponse:
            with self.engine.connect() as conn:
                row = conn.execute(select(stock).where(stock.c.sku == sku)).first()
            if row is None:
                return JSONResponse({"error": "sku_not_found", "sku": sku}, 404)
            data = dict(row._mapping)
            return JSONResponse({**data, "available": data["on_hand"] - data["reserved"]})

        @app.post("/reservations")
        async def create_reservation(body: CreateReservation, request: Request) -> JSONResponse:
            reservation_id = self.runtime.ids.new("res")

            def commit(conn: Connection) -> tuple[int, dict[str, Any]]:
                level = conn.execute(select(stock).where(stock.c.sku == body.sku)).first()
                if level is None:
                    raise Rejected(404, {"error": "sku_not_found", "sku": body.sku})
                if not self.flaws.accept_duplicates:
                    existing = conn.execute(
                        select(reservations.c.id).where(
                            reservations.c.order_id == body.order_id,
                            reservations.c.status == "active",
                        )
                    ).first()
                    if existing is not None:
                        raise Rejected(
                            409, {"error": "duplicate_reservation", "existing_id": existing.id}
                        )
                if level.on_hand - level.reserved < body.quantity:
                    raise Rejected(409, {"error": "insufficient_stock", "sku": body.sku})
                conn.execute(
                    insert(reservations).values(
                        id=reservation_id,
                        status="active",
                        created_at_ms=self.runtime.clock.now_ms(),
                        **body.model_dump(),
                    )
                )
                conn.execute(
                    update(stock)
                    .where(stock.c.sku == body.sku)
                    .values(reserved=stock.c.reserved + body.quantity)
                )
                return 201, self._reservation(conn, reservation_id)

            return self.mutate(
                request,
                "create_reservation",
                body.model_dump(),
                commit,
                pending_body={"id": reservation_id},
            )

        @app.get("/reservations/{reservation_id}")
        async def get_reservation(reservation_id: str) -> JSONResponse:
            with self.engine.connect() as conn:
                try:
                    return JSONResponse(self._reservation(conn, reservation_id))
                except Rejected as rejected:
                    return JSONResponse(rejected.body, rejected.status)

        @app.get("/reservations")
        async def list_reservations(order_id: str | None = None) -> list[dict[str, Any]]:
            query = select(reservations).order_by(reservations.c.id)
            if order_id is not None:
                query = query.where(reservations.c.order_id == order_id)
            with self.engine.connect() as conn:
                return [dict(row._mapping) for row in conn.execute(query)]

        @app.post("/reservations/{reservation_id}/release")
        async def release_reservation(reservation_id: str, request: Request) -> JSONResponse:
            def commit(conn: Connection) -> tuple[int, dict[str, Any]]:
                reservation = self._reservation(conn, reservation_id)
                if reservation["status"] == "released":
                    return 200, reservation
                conn.execute(
                    update(reservations)
                    .where(reservations.c.id == reservation_id)
                    .values(status="released", released_at_ms=self.runtime.clock.now_ms())
                )
                conn.execute(
                    update(stock)
                    .where(stock.c.sku == reservation["sku"])
                    .values(reserved=stock.c.reserved - reservation["quantity"])
                )
                return 200, self._reservation(conn, reservation_id)

            return self.mutate(
                request,
                "release_reservation",
                {"reservation_id": reservation_id},
                commit,
                pending_body={"id": reservation_id},
            )
