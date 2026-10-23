from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    EvaluateNeed,
    HiddenNeedData,
    NeedComparisons,
    NeedOptions,
    NeedPage,
    NeedView,
    SelectNeed,
)
from collective.api.errors import DomainError
from collective.domain.clock import SystemClock
from collective.services import needs
from collective.services.permissions import membership

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["needs"])
CommandKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


@router.get("/needs")
async def listing(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> NeedPage:
    async with db.transaction("tenant", principal.subject) as connection:
        await membership(connection, principal.subject, business_id)
        parameters = [business_id]
        query = "select id from collective.reorder_needs where business_id=%s"
        if cursor:
            row = await (
                await connection.execute(
                    """select created_at from collective.reorder_needs
                    where business_id=%s and id=%s""",
                    (business_id, cursor),
                )
            ).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Resource not found.", 404)
            query += " and (created_at,id)<(%s,%s)"
            parameters.extend((row["created_at"], cursor))
        query += " order by created_at desc,id desc limit %s"
        parameters.append(limit + 1)
        rows = await (await connection.execute(query, parameters)).fetchall()
        return NeedPage(
            items=tuple(
                [
                    needs.view(await needs.load_need(connection, business_id, r["id"]))
                    for r in rows[:limit]
                ]
            ),
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        )


@router.post("/needs/evaluate")
async def evaluate(
    business_id: UUID,
    body: EvaluateNeed,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> NeedOptions:
    async with db.transaction("tenant", principal.subject) as connection:
        await membership(connection, principal.subject, business_id, owner=True)
        return await needs.evaluate(
            connection, principal.subject, business_id, body, key, SystemClock().now()
        )


@router.get("/needs/{need_id}/options")
async def options(
    business_id: UUID, need_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> NeedOptions | HiddenNeedData:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        row = await needs.load_need(connection, business_id, need_id)
        if not access.can_view_prices:
            return HiddenNeedData(need=needs.view(row))
        return await needs.options(connection, row, SystemClock().now())


@router.get("/needs/{need_id}/comparisons")
async def comparisons(
    business_id: UUID, need_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> NeedComparisons | HiddenNeedData:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        row = await needs.load_need(connection, business_id, need_id)
        if not access.can_view_prices:
            return HiddenNeedData(need=needs.view(row))
        return await needs.comparisons(connection, row, SystemClock().now())


@router.post("/needs/{need_id}/select")
async def select(
    business_id: UUID,
    need_id: UUID,
    body: SelectNeed,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> NeedView:
    async with db.transaction("tenant", principal.subject) as connection:
        await membership(connection, principal.subject, business_id, owner=True)
        return await needs.select(
            connection, principal.subject, business_id, need_id, body, key, SystemClock().now()
        )
