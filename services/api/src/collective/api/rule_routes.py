from uuid import UUID

from fastapi import APIRouter, Query

from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import AggregateRule, JobStatus, Mutation, PrivateRule, RulePage
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.repositories import jobs
from collective.repositories.db import set_context
from collective.services import rules

router = APIRouter(tags=["rules"])


@router.get("/v1/businesses/{business_id}/rules")
async def private_listing(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> RulePage:
    async with db.transaction("tenant", principal.subject) as connection:
        return await rules.listing(
            connection,
            principal.subject,
            "private",
            business_id,
            cursor,
            limit,
            SystemClock().now(),
        )


@router.get("/v1/networks/{network_id}/rules")
async def network_listing(
    network_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> RulePage:
    async with db.transaction("tenant", principal.subject) as connection:
        return await rules.listing(
            connection, principal.subject, "network", network_id, cursor, limit, SystemClock().now()
        )


async def evaluate(db, subject, scope, identity, key):
    async with db.transaction("tenant", subject) as connection:
        await rules.authorize(connection, subject, scope, identity, owner=True)
        await set_context(connection, "coordinator", subject)
        now = SystemClock().now()
        job = await jobs.enqueue(
            connection,
            business=identity if scope == "private" else None,
            kind="rule_proposal",
            revision=1,
            key=f"rule_proposal:{scope}:{identity}:manual:{subject}:{key}:learning-v1",
            payload={"scope": scope, "scope_id": str(identity)},
            due=now,
        )
        return jobs.public_status(job, now)


@router.post("/v1/businesses/{business_id}/rules/evaluate")
async def private_evaluate(
    business_id: UUID, key: CommandKey, principal: SignedIn, db: DatabaseDependency
) -> JobStatus:
    return await evaluate(db, principal.subject, "private", business_id, key)


@router.post("/v1/networks/{network_id}/rules/evaluate")
async def network_evaluate(
    network_id: UUID, key: CommandKey, principal: SignedIn, db: DatabaseDependency
) -> JobStatus:
    return await evaluate(db, principal.subject, "network", network_id, key)


@router.post("/v1/businesses/{business_id}/rules/{rule_id}/{action}")
async def private_change(
    business_id: UUID,
    rule_id: UUID,
    action: str,
    body: Mutation,
    principal: SignedIn,
    db: DatabaseDependency,
) -> PrivateRule:
    return await change(db, principal.subject, "private", business_id, rule_id, action, body)


@router.post("/v1/networks/{network_id}/rules/{rule_id}/{action}")
async def network_change(
    network_id: UUID,
    rule_id: UUID,
    action: str,
    body: Mutation,
    principal: SignedIn,
    db: DatabaseDependency,
) -> AggregateRule:
    return await change(db, principal.subject, "network", network_id, rule_id, action, body)


async def change(db, subject, scope, identity, rule_id, action, body):
    from collective.api.errors import DomainError

    if action not in {"accept", "reject", "revoke"}:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    async with db.transaction("tenant", subject) as connection:
        return await rules.change(
            connection,
            subject,
            scope,
            identity,
            rule_id,
            action,
            body.expected_revision,
            SystemClock().now(),
        )
