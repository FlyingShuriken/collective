from uuid import UUID

from fastapi import APIRouter

from collective.adapters.hub import SERVICES, service
from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    ApprovalSummary,
    HiddenApproval,
    HubIdentity,
    HubService,
    Identity,
    VisibleApproval,
)
from collective.api.errors import DomainError
from collective.services.permissions import identity, membership

router = APIRouter(prefix="/v1", tags=["identity"])


@router.get("/me")
async def me(principal: SignedIn, db: DatabaseDependency) -> Identity:
    async with db.transaction("tenant", principal.subject) as connection:
        return await identity(connection, principal.subject)


@router.get("/hub/identity")
@router.get("/hub/roles")
async def hub_identity(principal: SignedIn, db: DatabaseDependency) -> HubIdentity:
    result = await me(principal, db)
    return HubIdentity(**result.model_dump())


@router.get("/hub/services")
async def services(principal: SignedIn) -> tuple[HubService, ...]:
    return SERVICES


@router.get("/hub/services/{name}")
async def service_detail(name: str, principal: SignedIn) -> HubService:
    return service(name)


@router.get("/hub/approvals/{approval_id}")
async def approval(
    approval_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> VisibleApproval | HiddenApproval:
    async with db.transaction("tenant", principal.subject) as connection:
        cursor = await connection.execute(
            "select * from collective.bound_approvals where id=%s", (approval_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Resource not found.", 404)
        access = await membership(connection, principal.subject, row["business_id"])
        summary = {key: row[key] for key in ApprovalSummary.model_fields}
        if access.can_view_prices:
            return VisibleApproval(**summary, cap=row["cap"], terms=row["terms"])
        return HiddenApproval(**summary)
