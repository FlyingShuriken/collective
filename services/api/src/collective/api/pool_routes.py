from uuid import UUID

from fastapi import APIRouter

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    DiscoverPool,
    DiscoveryResult,
    HiddenPoolOffer,
    PoolOffer,
    PoolSummary,
)
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.repositories.pools import own_offer
from collective.services.permissions import membership
from collective.services.pool_discovery import discover

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["pools"])


@router.post("/pools/discover")
async def discovery(
    business_id: UUID,
    body: DiscoverPool,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> DiscoveryResult:
    async with db.transaction("tenant", principal.subject) as connection:
        return await discover(connection, principal.subject, business_id, body, key, SystemClock())


@router.get("/pools/{pool_id}/offer")
async def offer(
    business_id: UUID, pool_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> PoolOffer | HiddenPoolOffer:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        result = await own_offer(connection, business_id, pool_id, SystemClock().now())
        if access.can_view_prices:
            return result
        return HiddenPoolOffer(**{key: getattr(result, key) for key in PoolSummary.model_fields})
