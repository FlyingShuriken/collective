from uuid import UUID

from fastapi import APIRouter, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import JobPage, JobStatus
from collective.api.errors import DomainError
from collective.domain.clock import SystemClock
from collective.repositories.jobs import public_status
from collective.services.permissions import membership

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["jobs"])


@router.get("/jobs")
async def listing(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> JobPage:
    async with db.transaction("tenant", principal.subject) as connection:
        await membership(connection, principal.subject, business_id)
        parameters = [business_id]
        query = "select * from collective.jobs where business_id=%s"
        if cursor:
            row = await (
                await connection.execute(
                    "select created_at from collective.jobs where business_id=%s and id=%s",
                    (business_id, cursor),
                )
            ).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Resource not found.", 404)
            query += " and (created_at,id)<(%s,%s)"
            parameters.extend((row["created_at"], cursor))
        query += " order by created_at desc,id desc limit %s"
        parameters.append(limit + 1)
        rows = await (await connection.execute(query, parameters)).fetchall()
        return JobPage(
            items=tuple(public_status(row, SystemClock().now()) for row in rows[:limit]),
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        )


@router.get("/jobs/{job_id}")
async def status(
    business_id: UUID, job_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> JobStatus:
    async with db.transaction("tenant", principal.subject) as connection:
        await membership(connection, principal.subject, business_id)
        row = await (
            await connection.execute(
                "select * from collective.jobs where business_id=%s and id=%s",
                (business_id, job_id),
            )
        ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Resource not found.", 404)
        return public_status(row, SystemClock().now())
