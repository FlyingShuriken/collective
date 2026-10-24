from uuid import UUID

from fastapi import APIRouter, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    EditQuoteProposal,
    HiddenProposal,
    JobStatus,
    Mutation,
    ParseQuote,
    ProposalPage,
    QuoteProposal,
    ReviewProposal,
)
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.services import proposals
from collective.services.liaison_jobs import request_quote
from collective.services.permissions import membership

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["proposals"])


@router.post("/quote-proposals")
async def create(
    business_id: UUID,
    body: ParseQuote,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> JobStatus:
    async with db.transaction("tenant", principal.subject) as connection:
        return await request_quote(
            connection, principal.subject, business_id, body, key, SystemClock().now()
        )


@router.get("/proposals")
async def listing(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> ProposalPage:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        parameters = [business_id]
        query = "select * from collective.proposals where business_id=%s and kind='quote'"
        if cursor:
            row, _ = await proposals.load(connection, business_id, cursor)
            query += " and (created_at,id)<(%s,%s)"
            parameters.extend((row["created_at"], cursor))
        query += " order by created_at desc,id desc limit %s"
        rows = await (await connection.execute(query, parameters + [limit + 1])).fetchall()
        items = []
        for row in rows[:limit]:
            row, revision = await proposals.load(connection, business_id, row["id"])
            items.append(await proposals.view(connection, row, revision, access))
        return ProposalPage(
            items=tuple(items), next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None
        )


@router.get("/proposals/{proposal_id}")
async def proposal(
    business_id: UUID, proposal_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> QuoteProposal | HiddenProposal:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        return await proposals.view(
            connection, *(await proposals.load(connection, business_id, proposal_id)), access
        )


@router.patch("/proposals/{proposal_id}")
async def edit(
    business_id: UUID,
    proposal_id: UUID,
    body: EditQuoteProposal,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> QuoteProposal:
    async with db.transaction("tenant", principal.subject) as connection:
        return await proposals.edit(
            connection, principal.subject, business_id, proposal_id, body, key
        )


@router.post("/proposals/{proposal_id}/confirm")
async def confirm(
    business_id: UUID,
    proposal_id: UUID,
    body: ReviewProposal,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> QuoteProposal:
    async with db.transaction("tenant", principal.subject) as connection:
        return await proposals.confirm(
            connection, principal.subject, business_id, proposal_id, body, key, SystemClock()
        )


@router.post("/proposals/{proposal_id}/reject")
async def reject(
    business_id: UUID,
    proposal_id: UUID,
    body: Mutation,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> QuoteProposal:
    async with db.transaction("tenant", principal.subject) as connection:
        return await proposals.reject(
            connection, principal.subject, business_id, proposal_id, body, key
        )
