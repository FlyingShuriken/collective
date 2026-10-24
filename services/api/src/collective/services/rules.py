import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from collective.api.dto import AggregateRule, HiddenRule, PrivateRule, RulePage, RuleTransition
from collective.api.errors import DomainError
from collective.domain.types import Snapshot
from collective.repositories import jobs, learning, pools
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.services.permissions import membership


class RuleJobInput(Snapshot):
    scope: Literal["private", "network"]
    scope_id: UUID


async def authorize(connection, subject, scope, identity, *, owner=False):
    if scope == "private":
        access = await membership(connection, subject, identity, owner=owner)
        return access.can_view_prices
    row = await (
        await connection.execute(
            "select network_id from collective.network_admins where network_id=%s and user_id=%s",
            (identity, subject),
        )
    ).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    return True


async def scope_lock(connection, scope, identity):
    await connection.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s,0))", (f"rule-scope:{scope}:{identity}",)
    )


def context_key(kind, context):
    return (
        kind
        + ":"
        + hashlib.sha256(
            json.dumps(context.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
    )


async def rule_event(connection, row, *, actor=None, proposed=False):
    ids = (
        [row["business_id"]]
        if row["scope"] == "private"
        else [
            b["id"]
            for b in await (
                await connection.execute(
                    "select id from collective.businesses where network_id=%s", (row["network_id"],)
                )
            ).fetchall()
        ]
    )
    for business in ids:
        await append_event(
            connection,
            Event(
                business_id=business,
                actor_id=actor if row["scope"] == "private" else None,
                actor_kind="owner" if actor and row["scope"] == "private" else "system",
                event_type="rule_" + row["state"],
                entity_id=row["id"],
                entity_revision=row["revision"],
                correlation_id=row["id"],
                summary="An evidence-based rule proposal awaits the required human review."
                if proposed
                else "A human changed an evidence-based rule; open decisions were re-evaluated.",
            ),
        )


async def propose(connection, scope, identity, as_of):
    await scope_lock(connection, scope, identity)
    calculations, references = await learning.collect(connection, scope, identity, as_of)
    result = []
    for calculation in calculations:
        value = calculation.model_dump(mode="json")
        if scope == "network":
            value.pop("evidence")
        rule_id = uuid4()
        row = await (
            await connection.execute(
                """insert into collective.learned_rules
            (id,business_id,network_id,scope,kind,context_key,evidence_digest,proposed_value,
             sample_count,distinct_businesses) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict do nothing returning *""",
                (
                    rule_id,
                    identity if scope == "private" else None,
                    identity if scope == "network" else None,
                    scope,
                    calculation.value.kind,
                    context_key(calculation.value.kind, calculation.context),
                    calculation.evidence_digest,
                    Jsonb(value),
                    calculation.sample_count,
                    calculation.distinct_businesses,
                ),
            )
        ).fetchone()
        if row is None:
            continue
        sources = set()
        for reference in calculation.evidence:
            if (reference.business_id, reference.source_id) in sources:
                continue
            sources.add((reference.business_id, reference.source_id))
            purchase, revision = references[reference]
            await connection.execute(
                """insert into collective.rule_evidence
                (rule_id,business_id,source_id,purchase_id,purchase_revision)
                values (%s,%s,%s,%s,%s)""",
                (rule_id, reference.business_id, reference.source_id, purchase, revision),
            )
        row = await (
            await connection.execute(
                """update collective.learned_rules set sealed=true,revision=revision+1
            where id=%s returning *""",
                (rule_id,),
            )
        ).fetchone()
        await rule_event(connection, row, proposed=True)
        result.append(rule_id)
    return result


async def enqueue(connection, business, entity, revision, as_of):
    """One deduplicated private and network calculation after a confirmed record/check."""
    row = await (
        await connection.execute(
            "select network_id from collective.businesses where id=%s",
            (business,),
        )
    ).fetchone()
    targets = [("private", business)]
    if row["network_id"]:
        targets.append(("network", row["network_id"]))
    for scope, identity in targets:
        await jobs.enqueue(
            connection,
            business=business if scope == "private" else None,
            kind="rule_proposal",
            revision=revision,
            key=f"rule_proposal:{scope}:{identity}:{entity}:{revision}:learning-v1",
            payload={"scope": scope, "scope_id": str(identity)},
            due=as_of,
        )


async def handle(database, job, clock):
    payload = RuleJobInput.model_validate(job["payload"])
    async with database.transaction("coordinator", None) as connection:
        await jobs.hold_claim(connection, job, clock.now())
        ids = await propose(connection, payload.scope, payload.scope_id, clock.now())
        await jobs.complete(connection, job, {"proposal_count": len(ids)}, clock.now())


def view(row, *, can_view_prices=True):
    data = row["proposed_value"]
    history = []
    for item in [*row["history"].values(), row]:
        if item.get("reviewed_by"):
            history.append(
                RuleTransition(**{key: item[key] for key in RuleTransition.model_fields})
            )
    summary = {
        key: row[key]
        for key in (
            "id",
            "revision",
            "scope",
            "kind",
            "state",
            "sample_count",
            "distinct_businesses",
        )
    }
    summary |= {
        key: data[key]
        for key in (
            "period_start",
            "period_end",
            "uncertainty",
            "excluded_count",
        )
    }
    summary |= {
        "eligible": data["eligible_private" if row["scope"] == "private" else "eligible_network"],
        "history": sorted(history, key=lambda item: item.revision),
    }
    if not can_view_prices:
        return HiddenRule(**summary)
    if row["scope"] == "network":
        return AggregateRule(**summary, context=data["context"], value=data["value"])
    return PrivateRule(
        **summary, context=data["context"], value=data["value"], evidence=data["evidence"]
    )


async def listing(connection, subject, scope, identity, cursor, limit, as_of):
    prices = await authorize(connection, subject, scope, identity)
    column = "business_id" if scope == "private" else "network_id"
    query = f"select * from collective.learned_rules where scope=%s and {column}=%s and sealed"
    values = [scope, identity]
    if scope == "network":
        query += " and distinct_businesses>=5"
    if cursor:
        prior = await (await connection.execute(query + " and id=%s", values + [cursor])).fetchone()
        if prior is None:
            raise DomainError("NOT_FOUND", "Resource not found.", 404)
        query += " and (created_at,id)<(%s,%s)"
        values.extend((prior["created_at"], cursor))
    rows = await (
        await connection.execute(
            query + " order by created_at desc,id desc limit %s",
            values + [limit + 1],
        )
    ).fetchall()
    await set_context(connection, "coordinator", subject)
    job = await (
        await connection.execute(
            """select * from collective.jobs where kind='rule_proposal'
        and payload->>'scope'=%s and payload->>'scope_id'=%s
        order by created_at desc,id desc limit 1""",
            (scope, str(identity)),
        )
    ).fetchone()
    await set_context(connection, "tenant", subject)
    return RulePage(
        items=[view(row, can_view_prices=prices) for row in rows[:limit]],
        next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        latest_job=jobs.public_status(job, as_of) if job else None,
    )


async def change(connection, subject, scope, identity, rule_id, action, expected_revision, as_of):
    await authorize(connection, subject, scope, identity, owner=True)
    await set_context(connection, "coordinator", subject)
    await scope_lock(connection, scope, identity)
    row = await (
        await connection.execute(
            """select * from collective.learned_rules where id=%s and scope=%s and sealed and
        ((scope='private' and business_id=%s) or (scope='network' and network_id=%s))""",
            (rule_id, scope, identity, identity),
        )
    ).fetchone()
    if row is None or (scope == "network" and row["distinct_businesses"] < 5):
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    # Lock every affected purchasing context before rules or approvals, in the shared order.
    specs = await (
        await connection.execute(
            """select distinct n.spec_id from collective.reorder_needs n
        join collective.businesses b on b.id=n.business_id where
        ((%s='private' and b.id=%s) or (%s='network' and b.network_id=%s))""",
            (scope, identity, scope, identity),
        )
    ).fetchall()
    specs = [r["spec_id"] for r in specs]
    approvals = await pools.lock_record_inputs(connection, specs)
    await set_context(connection, "tenant", subject)
    await authorize(connection, subject, scope, identity, owner=True)
    row = await (
        await connection.execute(
            "select * from collective.learned_rules where id=%s for update",
            (rule_id,),
        )
    ).fetchone()
    target = {"accept": "accepted", "reject": "rejected", "revoke": "revoked"}[action]
    if row["revision"] != expected_revision:
        if (
            row["revision"] == expected_revision + 1
            and row["state"] == target
            and row["reviewed_by"] == subject
        ):
            return view(row)
        raise DomainError("STALE_REVISION", "Review the latest rule revision.", 409)
    if row["state"] != ("accepted" if action == "revoke" else "proposed"):
        raise DomainError("RULE_FINAL", "This rule no longer permits that action.", 409)
    if action == "accept":
        if not row["proposed_value"][
            "eligible_private" if scope == "private" else "eligible_network"
        ]:
            raise DomainError(
                "INSUFFICIENT_EVIDENCE", "More comparable confirmed evidence is required.", 409
            )
        await set_context(connection, "coordinator", subject)
        current, _ = await learning.collect(connection, scope, identity, as_of)
        if not any(c.evidence_digest == row["evidence_digest"] for c in current):
            raise DomainError(
                "STALE_EVIDENCE", "Evidence changed. Generate and review a fresh proposal.", 409
            )
        await set_context(connection, "tenant", subject)
        previous = await (
            await connection.execute(
                """update collective.learned_rules
            set state='superseded',reviewed_by=%s,reviewed_at=%s,
            revision=revision+1 where state='accepted' and kind=%s and context_key=%s and scope=%s
            and ((scope='private' and business_id=%s) or (scope='network' and network_id=%s))
            returning *""",
                (subject, as_of, row["kind"], row["context_key"], scope, identity, identity),
            )
        ).fetchall()
    else:
        previous = []
    row = await (
        await connection.execute(
            """update collective.learned_rules set state=%s,reviewed_by=%s,reviewed_at=%s,
        accepted_by=case when %s='accepted' then %s else accepted_by end,
        accepted_at=case when %s='accepted' then %s else accepted_at end,revision=revision+1
        where id=%s returning *""",
            (target, subject, as_of, target, subject, target, as_of, rule_id),
        )
    ).fetchone()
    await set_context(connection, "coordinator", subject)
    from collective.services import needs, solo
    from collective.services.pool_commands import recalculate

    for pool in await (
        await connection.execute(
            """select * from collective.pools
            where spec_id=any(%s) and state<>'released' order by id""",
            (specs,),
        )
    ).fetchall():
        await recalculate(connection, pool, as_of)
    for approval in approvals:
        if approval["pool_id"] is None and approval["state"] == "valid":
            need = await needs.load_need(connection, approval["business_id"], approval["need_id"])
            evaluation = await solo.evaluate(connection, need, approval["quote_id"], as_of)
            await solo.revalidate(connection, approval, evaluation, as_of)
    for changed in [*previous, row]:
        await rule_event(connection, changed, actor=subject)
    return view(row)
