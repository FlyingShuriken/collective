from uuid import UUID

from fastapi import APIRouter, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    DocumentProposal,
    EditDocumentProposal,
    EditQuoteProposal,
    HiddenProposal,
    JobStatus,
    Mutation,
    ParseQuote,
    ProposalPage,
    QuoteProposal,
    RecordOptions,
    ReviewProposal,
)
from collective.api.errors import DomainError
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
        query = """select * from collective.proposals where business_id=%s
            and kind in ('quote','purchase','stock')"""
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
) -> QuoteProposal | DocumentProposal | HiddenProposal:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        return await proposals.view(
            connection, *(await proposals.load(connection, business_id, proposal_id)), access
        )


@router.patch("/proposals/{proposal_id}")
async def edit(
    business_id: UUID,
    proposal_id: UUID,
    body: EditQuoteProposal | EditDocumentProposal,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> QuoteProposal | DocumentProposal:
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
) -> QuoteProposal | DocumentProposal:
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
) -> QuoteProposal | DocumentProposal:
    async with db.transaction("tenant", principal.subject) as connection:
        return await proposals.reject(
            connection, principal.subject, business_id, proposal_id, body, key
        )


@router.get("/record-options")
async def record_options(
    business_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> RecordOptions:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        if not access.can_view_prices:
            raise DomainError("SOURCE_RESTRICTED", "This account cannot review source prices.", 403)
        suppliers = await (
            await connection.execute(
                """select s.id,s.name from collective.suppliers s
            join collective.business_suppliers b on b.supplier_id=s.id
            where b.business_id=%s order by s.name,s.id""",
                (business_id,),
            )
        ).fetchall()
        mappings = await (
            await connection.execute(
                """select m.id,m.revision,m.supplier_id,m.supplier_sku,m.source_name,
            m.spec_id,m.spec_revision,c.name as spec_name,s.base_unit from collective.sku_mappings m
            join collective.spec_revisions s on (s.spec_id,s.revision)=(m.spec_id,m.spec_revision)
            join collective.canonical_specs c on c.id=m.spec_id
            where m.business_id=%s and m.confirmed and not exists(
                select 1 from collective.sku_mappings n where n.business_id=m.business_id
                  and n.supplier_id=m.supplier_id and n.supplier_sku=m.supplier_sku
                  and n.confirmed and n.revision>m.revision)
            order by m.source_name,m.id""",
                (business_id,),
            )
        ).fetchall()
        return RecordOptions(suppliers=suppliers, mappings=mappings)
