from uuid import UUID

from fastapi import APIRouter, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import ActivityPage
from collective.api.errors import DomainError
from collective.services.permissions import membership

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["activity"])


@router.get("/events")
async def events(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> ActivityPage:
    async with db.transaction("tenant", principal.subject) as connection:
        await membership(connection, principal.subject, business_id)
        parameters = [business_id]
        query = """select id,event_type,entity_id,entity_revision,actor_kind,occurred_at,summary
        from collective.events where business_id=%s"""
        if cursor:
            anchor = await (
                await connection.execute(
                    "select occurred_at from collective.events where business_id=%s and id=%s",
                    (business_id, cursor),
                )
            ).fetchone()
            if anchor is None:
                raise DomainError("NOT_FOUND", "Resource not found.", 404)
            query += " and (occurred_at,id)<(%s,%s)"
            parameters.extend((anchor["occurred_at"], cursor))
        query += " order by occurred_at desc,id desc limit %s"
        parameters.append(limit + 1)
        rows = await (await connection.execute(query, parameters)).fetchall()
        return ActivityPage(
            items=tuple(rows[:limit]),
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        )
