from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import LedgerPage
from collective.repositories.ledger import ledger_page
from collective.services.permissions import membership

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["ledger"])


@router.get("/ledger")
async def ledger(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    kind: Literal["purchase", "stock", "quote", "po"] = "purchase",
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> LedgerPage:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        return LedgerPage.model_validate(
            await ledger_page(connection, business_id, kind, cursor, limit, access.can_view_prices)
        )
