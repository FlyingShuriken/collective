from uuid import UUID

from fastapi import APIRouter

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import ConfirmMapping, JobStatus, MappingPage, MappingView, RequestMapping
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.services import mappings
from collective.services.permissions import membership

router = APIRouter(prefix="/v1/businesses/{business_id}/mappings", tags=["mappings"])


@router.get("")
async def listing(business_id: UUID, principal: SignedIn, db: DatabaseDependency) -> MappingPage:
    async with db.transaction("tenant", principal.subject) as connection:
        await membership(connection, principal.subject, business_id, owner=True)
        rows = await (
            await connection.execute(
                """select m.* from collective.sku_mappings m where business_id=%s
            and not exists(select 1 from collective.sku_mappings n
            where n.business_id=m.business_id and n.id=m.id and n.revision>m.revision)
            order by created_at desc,id desc limit 100""",
                (business_id,),
            )
        ).fetchall()
        return MappingPage(items=tuple(mappings.view(row) for row in rows))


@router.post("")
async def request(
    business_id: UUID,
    body: RequestMapping,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> JobStatus:
    async with db.transaction("tenant", principal.subject) as connection:
        return await mappings.request(
            connection, principal.subject, business_id, body, key, SystemClock().now()
        )


@router.post("/{mapping_id}/confirm")
async def confirm(
    business_id: UUID,
    mapping_id: UUID,
    body: ConfirmMapping,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> MappingView:
    async with db.transaction("tenant", principal.subject) as connection:
        return await mappings.confirm(
            connection, principal.subject, business_id, mapping_id, body, key, SystemClock().now()
        )
