from uuid import UUID

from fastapi import APIRouter

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import ApprovalCommandResult, ApproveOrder, SoloExecution
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.services import solo
from collective.services.approval_commands import approve

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["approvals"])


@router.post("/approvals")
async def approval(
    business_id: UUID,
    body: ApproveOrder,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> ApprovalCommandResult:
    async with db.transaction("tenant", principal.subject) as connection:
        return await approve(connection, principal.subject, business_id, body, key, SystemClock())


@router.post("/needs/{need_id}/withdraw")
async def solo_withdraw(
    business_id: UUID,
    need_id: UUID,
    body: SoloExecution,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> ApprovalCommandResult:
    async with db.transaction("tenant", principal.subject) as connection:
        return await solo.withdraw(
            connection, principal.subject, business_id, need_id, body, key, SystemClock()
        )
