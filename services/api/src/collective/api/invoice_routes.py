from uuid import UUID

from fastapi import APIRouter, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    HiddenInvoiceCheck,
    InvoiceCheckPage,
    InvoiceOrderOptions,
    JobStatus,
    RequestInvoiceCheck,
    ReviewInvoiceCheck,
    VisibleInvoiceCheck,
)
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.repositories.jobs import public_status
from collective.services import invoice_checks
from collective.services.permissions import membership

router = APIRouter(prefix="/v1/businesses/{business_id}/invoice-checks", tags=["invoices"])


@router.post("")
async def create(
    business_id: UUID,
    body: RequestInvoiceCheck,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> JobStatus:
    async with db.transaction("tenant", principal.subject) as connection:
        return await invoice_checks.request(
            connection, principal.subject, business_id, body, key, SystemClock().now()
        )


@router.get("")
async def listing(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    purchase_id: UUID | None = None,
    purchase_revision: int | None = Query(default=None, ge=1),
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> InvoiceCheckPage:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        query, parameters = (
            "select id from collective.invoice_checks where business_id=%s",
            [business_id],
        )
        if purchase_id:
            query += " and purchase_id=%s"
            parameters.append(purchase_id)
        if purchase_revision:
            query += " and purchase_revision=%s"
            parameters.append(purchase_revision)
        if cursor:
            row = await invoice_checks.load(connection, business_id, cursor)
            query += " and (created_at,id)<(%s,%s)"
            parameters.extend((row["created_at"], cursor))
        query += " order by created_at desc,id desc limit %s"
        rows = await (await connection.execute(query, parameters + [limit + 1])).fetchall()
        items = []
        for row in rows[:limit]:
            items.append(
                invoice_checks.view(
                    await invoice_checks.load(connection, business_id, row["id"]), access
                )
            )
        job = None
        if purchase_id:
            job_query = """select * from collective.jobs where business_id=%s
                and kind='invoice_check' and payload->>'purchase_id'=%s"""
            job_parameters = [business_id, str(purchase_id)]
            if purchase_revision:
                job_query += " and input_revision=%s"
                job_parameters.append(purchase_revision)
            job = await (
                await connection.execute(
                    job_query + " order by created_at desc,id desc limit 1",
                    job_parameters,
                )
            ).fetchone()
        return InvoiceCheckPage(
            items=items,
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
            latest_job=public_status(job, SystemClock().now()) if job else None,
        )


@router.get("/order-options")
async def options(
    business_id: UUID,
    purchase_id: UUID,
    purchase_revision: int,
    principal: SignedIn,
    db: DatabaseDependency,
) -> InvoiceOrderOptions:
    async with db.transaction("tenant", principal.subject) as connection:
        await invoice_checks.reviewer(connection, principal.subject, business_id)
        invoice = await invoice_checks.purchase(
            connection, business_id, purchase_id, purchase_revision
        )
        rows = await (
            await connection.execute(
                """select id,spec_id,spec_revision,quantity,amount,issued_at
                from collective.purchase_orders
            where business_id=%s and supplier_id=%s order by issued_at desc,id limit 100""",
                (business_id, invoice["supplier_id"]),
            )
        ).fetchall()
        return InvoiceOrderOptions(orders=rows)


@router.get("/{check_id}")
async def check(
    business_id: UUID, check_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> VisibleInvoiceCheck | HiddenInvoiceCheck:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        return invoice_checks.view(
            await invoice_checks.load(connection, business_id, check_id), access
        )


@router.post("/{check_id}/review")
async def review(
    business_id: UUID,
    check_id: UUID,
    body: ReviewInvoiceCheck,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> VisibleInvoiceCheck:
    async with db.transaction("tenant", principal.subject) as connection:
        return await invoice_checks.review(
            connection, principal.subject, business_id, check_id, body, key
        )
