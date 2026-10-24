from uuid import UUID

from fastapi import APIRouter, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    DraftPage,
    DraftView,
    EditDraft,
    HiddenDraft,
    JobStatus,
    MessageReceipt,
    RequestDraft,
    SendDraft,
)
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.services import drafts
from collective.services.liaison_jobs import request_draft
from collective.services.permissions import membership

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["drafts"])


@router.post("/drafts")
async def create(
    business_id: UUID,
    body: RequestDraft,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> JobStatus:
    async with db.transaction("tenant", principal.subject) as connection:
        return await request_draft(
            connection, principal.subject, business_id, body, key, SystemClock().now()
        )


@router.get("/drafts")
async def listing(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    cursor: UUID | None = None,
    reference_id: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> DraftPage:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        parameters = [business_id]
        query = "select * from collective.message_drafts where business_id=%s"
        if reference_id:
            query += " and input_context->'request'->>'reference_id'=%s"
            parameters.append(str(reference_id))
        if cursor:
            row = await drafts.load(connection, business_id, cursor)
            query += " and (created_at,id)<(%s,%s)"
            parameters.extend((row["created_at"], cursor))
        query += " order by created_at desc,id desc limit %s"
        rows = await (await connection.execute(query, parameters + [limit + 1])).fetchall()
        return DraftPage(
            items=tuple(drafts.view(row, access) for row in rows[:limit]),
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        )


@router.get("/drafts/{draft_id}")
async def draft(
    business_id: UUID,
    draft_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
) -> DraftView | HiddenDraft:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        return drafts.view(await drafts.load(connection, business_id, draft_id), access)


@router.patch("/drafts/{draft_id}")
async def edit(
    business_id: UUID,
    draft_id: UUID,
    body: EditDraft,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> DraftView:
    async with db.transaction("tenant", principal.subject) as connection:
        return await drafts.edit(connection, principal.subject, business_id, draft_id, body, key)


@router.post("/drafts/{draft_id}/send")
async def send(
    business_id: UUID,
    draft_id: UUID,
    body: SendDraft,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> MessageReceipt:
    async with db.transaction("tenant", principal.subject) as connection:
        return await drafts.send(
            connection, principal.subject, business_id, draft_id, body, key, SystemClock().now()
        )
