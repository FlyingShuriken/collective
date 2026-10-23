from uuid import UUID

from fastapi import APIRouter

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    HiddenPurchaseOrder,
    Mutation,
    ReleaseResult,
    SoloExecution,
    VisiblePurchaseOrder,
)
from collective.api.errors import DomainError
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.repositories.orders import own_order
from collective.services.permissions import membership
from collective.services.releases import release_pool, release_solo

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["orders"])


@router.post("/pools/{pool_id}/release")
async def release(
    business_id: UUID,
    pool_id: UUID,
    body: Mutation,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> ReleaseResult:
    async with db.transaction("tenant", principal.subject) as connection:
        return await release_pool(
            connection, principal.subject, business_id, pool_id, body, key, SystemClock()
        )


@router.get("/needs/{need_id}/order")
async def order(
    business_id: UUID,
    need_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
) -> VisiblePurchaseOrder | HiddenPurchaseOrder:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        result = await own_order(connection, business_id, need_id, prices=access.can_view_prices)
        if result is None:
            raise DomainError("NOT_FOUND", "Resource not found.", 404)
        return result


@router.post("/needs/{need_id}/release")
async def solo_release(
    business_id: UUID,
    need_id: UUID,
    body: SoloExecution,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> ReleaseResult:
    async with db.transaction("tenant", principal.subject) as connection:
        return await release_solo(
            connection, principal.subject, business_id, need_id, body, key, SystemClock()
        )
