"""Billing service: create and void invoices."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, Connection, Integer, MetaData, String, Table, insert, select, update

from agentchaos.services.base import MockService, Rejected, Unchanged


class CreateInvoice(BaseModel):
    order_id: str
    amount_cents: int = Field(ge=0)


class BillingService(MockService):
    name = "billing"

    def define_tables(self, metadata: MetaData) -> None:
        self.invoices = Table(
            "invoices",
            metadata,
            Column("id", String, primary_key=True),
            Column("order_id", String, nullable=False),
            Column("amount_cents", Integer, nullable=False),
            Column("status", String, nullable=False),  # issued | void
            Column("created_at_ms", Integer, nullable=False),
            Column("voided_at_ms", Integer, nullable=True),
        )

    def _invoice(self, conn: Connection, invoice_id: str) -> dict[str, Any]:
        row = conn.execute(select(self.invoices).where(self.invoices.c.id == invoice_id)).first()
        if row is None:
            raise Rejected(404, {"error": "invoice_not_found", "id": invoice_id})
        return dict(row._mapping)

    def define_routes(self, app: FastAPI) -> None:
        invoices = self.invoices

        @app.post("/invoices")
        async def create_invoice(body: CreateInvoice, request: Request) -> JSONResponse:
            invoice_id = self.runtime.ids.new("inv")

            # Billing does not check that the order has a reservation.
            def commit(conn: Connection) -> tuple[int, dict[str, Any]]:
                if not self.flaws.accept_duplicates:
                    existing = conn.execute(
                        select(invoices.c.id).where(
                            invoices.c.order_id == body.order_id, invoices.c.status == "issued"
                        )
                    ).first()
                    if existing is not None:
                        raise Rejected(
                            409, {"error": "duplicate_invoice", "existing_id": existing.id}
                        )
                conn.execute(
                    insert(invoices).values(
                        id=invoice_id,
                        status="issued",
                        created_at_ms=self.runtime.clock.now_ms(),
                        **body.model_dump(),
                    )
                )
                return 201, self._invoice(conn, invoice_id)

            return self.mutate(
                request,
                "create_invoice",
                body.model_dump(),
                commit,
                pending_body={"id": invoice_id},
            )

        @app.get("/invoices/{invoice_id}")
        async def get_invoice(invoice_id: str) -> JSONResponse:
            with self.engine.connect() as conn:
                try:
                    return JSONResponse(self._invoice(conn, invoice_id))
                except Rejected as rejected:
                    return JSONResponse(rejected.body, rejected.status)

        @app.get("/invoices")
        async def list_invoices(order_id: str | None = None) -> list[dict[str, Any]]:
            query = select(invoices).order_by(invoices.c.id)
            if order_id is not None:
                query = query.where(invoices.c.order_id == order_id)
            with self.engine.connect() as conn:
                return [dict(row._mapping) for row in conn.execute(query)]

        @app.post("/invoices/{invoice_id}/void")
        async def void_invoice(invoice_id: str, request: Request) -> JSONResponse:
            def commit(conn: Connection) -> tuple[int, dict[str, Any]]:
                invoice = self._invoice(conn, invoice_id)
                if invoice["status"] == "void":
                    raise Unchanged(200, invoice)
                conn.execute(
                    update(invoices)
                    .where(invoices.c.id == invoice_id)
                    .values(status="void", voided_at_ms=self.runtime.clock.now_ms())
                )
                return 200, self._invoice(conn, invoice_id)

            return self.mutate(
                request,
                "void_invoice",
                {"invoice_id": invoice_id},
                commit,
                pending_body={"id": invoice_id},
            )
