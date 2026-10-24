from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from collective.agents import liaison
from collective.agents.schemas import QuoteReading
from collective.api.dto import HiddenProposal, ProposalSummary, QuoteProposal
from collective.api.errors import DomainError
from collective.repositories import pools
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services import needs, solo
from collective.services.liaison_jobs import supplier
from collective.services.permissions import membership
from collective.services.pool_commands import recalculate


async def load(connection, business, identity, *, lock=False):
    query = "select * from collective.proposals where business_id=%s and id=%s and kind='quote'"
    row = await (
        await connection.execute(query + (" for update" if lock else ""), (business, identity))
    ).fetchone()
    if row is None and lock:
        row = await (await connection.execute(query, (business, identity))).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    revision = await (
        await connection.execute(
            """select * from collective.proposal_revisions
        where business_id=%s and proposal_id=%s and revision=%s""",
            (business, identity, row["current_revision"]),
        )
    ).fetchone()
    return row, revision


async def view(connection, row, revision, access):
    summary = {key: row[key] for key in ProposalSummary.model_fields}
    if not access.can_view_prices:
        return HiddenProposal(**summary)
    record = await (
        await connection.execute(
            """select id,revision from collective.quote_revisions
        where business_id=%s and proposal_id=%s and proposal_revision=%s""",
            (row["business_id"], row["id"], row["current_revision"]),
        )
    ).fetchone()
    return QuoteProposal(
        **summary,
        **revision["payload"],
        flags=revision["flags"],
        evidence=revision["evidence"],
        model=revision["model"],
        prompt_version=revision["prompt_version"],
        schema_version=revision["schema_version"],
        record_id=record["id"] if record else None,
        record_revision=record["revision"] if record else None,
    )


async def editable(connection, subject, business, identity):
    access = await membership(connection, subject, business)
    if not access.can_view_prices:
        raise DomainError("SOURCE_RESTRICTED", "This account cannot review quote prices.", 403)
    return access


async def event(connection, subject, access, row, kind):
    await append_event(
        connection,
        Event(
            business_id=row["business_id"],
            actor_id=subject,
            actor_kind=access.role,
            event_type=kind,
            entity_id=row["id"],
            entity_revision=row["current_revision"],
            correlation_id=row["id"],
            summary={
                "proposal_edited": "A reviewer edited a supplier quote proposal.",
                "proposal_rejected": "A reviewer rejected a supplier quote proposal.",
                "quote_confirmed": "A reviewer confirmed a supplier quote record.",
            }[kind],
        ),
    )


def require_pending(row, expected):
    if row["current_revision"] != expected:
        raise DomainError("STALE_REVISION", "Review the latest proposal revision.", 409)
    if row["status"] != "pending_review":
        raise DomainError("PROPOSAL_CLOSED", "This proposal is already confirmed or rejected.", 409)


async def edit(connection, subject, business, identity, body, key):
    access = await editable(connection, subject, business, identity)
    route, payload = f"proposals/{identity}/edit", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        return QuoteProposal.model_validate(prior)
    row, revision = await load(connection, business, identity, lock=True)
    require_pending(row, body.expected_revision)
    reading = QuoteReading.model_validate(revision["payload"]["reading"])
    if reading.evidence != body.reading.evidence:
        raise DomainError(
            "EVIDENCE_IMMUTABLE", "Keep the saved source evidence when editing fields."
        )
    flags = liaison.review_flags(body.reading)
    if "evidence:review" in revision["flags"]:
        flags.append("evidence:review")
    number = row["current_revision"] + 1
    await connection.execute(
        """insert into collective.proposal_revisions
        (business_id,proposal_id,revision,payload,evidence,flags,agent,model,prompt_version,
         schema_version,edited_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            business,
            identity,
            number,
            Jsonb(revision["payload"] | {"reading": body.reading.model_dump(mode="json")}),
            Jsonb(revision["evidence"]),
            Jsonb(flags),
            revision["agent"],
            revision["model"],
            revision["prompt_version"],
            revision["schema_version"],
            subject,
        ),
    )
    await connection.execute(
        """update collective.proposals set current_revision=%s
        where business_id=%s and id=%s""",
        (number, business, identity),
    )
    row, revision = await load(connection, business, identity)
    await event(connection, subject, access, row, "proposal_edited")
    result = await view(connection, row, revision, access)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result


async def reject(connection, subject, business, identity, body, key):
    access = await editable(connection, subject, business, identity)
    route, payload = f"proposals/{identity}/reject", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        return QuoteProposal.model_validate(prior)
    row, revision = await load(connection, business, identity, lock=True)
    if row["status"] != "rejected":
        require_pending(row, body.expected_revision)
        await connection.execute(
            "update collective.proposals set status='rejected' where id=%s", (identity,)
        )
        row["status"] = "rejected"
        await event(connection, subject, access, row, "proposal_rejected")
    elif row["current_revision"] != body.expected_revision:
        raise DomainError("STALE_REVISION", "Review the latest proposal revision.", 409)
    result = await view(connection, row, revision, access)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result


async def confirm(connection, subject, business, identity, body, key, clock):
    access = await editable(connection, subject, business, identity)
    if access.role != "owner":
        # A quote has no bounded transaction amount for the staff confirmation limit.
        raise DomainError("OWNER_REVIEW_REQUIRED", "An owner must confirm these quote terms.", 403)
    row, revision = await load(connection, business, identity)
    context = revision["payload"]["context"]
    spec, supplier_id = UUID(context["spec_id"]), UUID(context["supplier_id"])
    await set_context(connection, "coordinator", subject)
    await pools.lock_inputs(connection, spec, extra_suppliers=(supplier_id,))
    approvals = await (
        await connection.execute(
            """select a.* from collective.bound_approvals a join collective.reorder_needs n
        on (n.business_id,n.id)=(a.business_id,a.need_id)
        where n.spec_id=%s order by a.id for update of a""",
            (spec,),
        )
    ).fetchall()
    await set_context(connection, "tenant", subject)
    await membership(connection, subject, business, owner=True)
    route, payload = f"proposals/{identity}/confirm", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        return QuoteProposal.model_validate(prior)
    row, revision = await load(connection, business, identity, lock=True)
    if row["status"] == "confirmed":
        result = await view(connection, row, revision, access)
        if (
            body.expected_revision != row["current_revision"]
            or (
                body.quote_id
                and (
                    body.quote_id != result.record_id
                    or body.expected_quote_revision + 1 != result.record_revision
                )
            )
            or (body.quote_id is None and result.record_revision != 1)
        ):
            raise DomainError(
                "STALE_REVISION", "Review the already confirmed quote reference.", 409
            )
    else:
        require_pending(row, body.expected_revision)
        if not set(revision["flags"]) <= set(body.acknowledged_flags):
            raise DomainError("REVIEW_FLAGS", "Review and acknowledge the unresolved fields.")
        reading = QuoteReading.model_validate(revision["payload"]["reading"])
        if any(
            getattr(reading, name) is None for name in ("base_unit", "pack_multiple", "tier_kind")
        ):
            raise DomainError(
                "MISSING_RECORD_FIELDS", "Confirm the quote's units, pack and tier kind first."
            )
        await supplier(connection, business, supplier_id)
        identity_quote, number, family = uuid4(), 1, uuid4()
        if body.quote_id:
            previous = await (
                await connection.execute(
                    """select * from collective.quote_revisions where business_id=%s and id=%s
                and sealed order by revision desc limit 1""",
                    (business, body.quote_id),
                )
            ).fetchone()
            if previous is None:
                raise DomainError("NOT_FOUND", "Resource not found.", 404)
            if (
                previous["revision"] != body.expected_quote_revision
                or previous["spec_id"] != spec
                or previous["supplier_id"] != supplier_id
            ):
                raise DomainError("STALE_REVISION", "Review the current quote context.", 409)
            identity_quote, number, family = (
                previous["id"],
                previous["revision"] + 1,
                previous["offer_family"],
            )
        zone = (
            await (
                await connection.execute(
                    "select zone from collective.businesses where id=%s",
                    (business,),
                )
            ).fetchone()
        )["zone"]
        excluded = {
            "description",
            "validity_text",
            "issues",
            "evidence",
            "confidence_label",
            "tiers",
        }
        values = {key: value for key, value in reading.model_dump().items() if key not in excluded}
        values |= {
            "business_id": business,
            "id": identity_quote,
            "revision": number,
            "supplier_id": supplier_id,
            "spec_id": spec,
            "spec_revision": context["spec_revision"],
            "source_id": row["source_id"],
            "proposal_id": identity,
            "proposal_revision": row["current_revision"],
            "offer_family": family,
            "zone": zone,
            "confirmed_by": subject,
            "provenance": "human",
        }
        unresolved = set(liaison.review_flags(reading)) - {"reading:review"}
        values["conditions_complete"] = reading.conditions_complete and not unresolved
        await connection.execute(
            sql.SQL("insert into collective.quote_revisions ({}) values ({})").format(
                sql.SQL(",").join(map(sql.Identifier, values)),
                sql.SQL(",").join(sql.Placeholder() for _ in values),
            ),
            list(values.values()),
        )
        for tier in reading.tiers:
            await connection.execute(
                """insert into collective.quote_tiers
                (business_id,quote_id,quote_revision,min_quantity,unit_price)
                values (%s,%s,%s,%s,%s)""",
                (business, identity_quote, number, tier.min_quantity, tier.unit_price),
            )
        await connection.execute(
            """update collective.quote_revisions set sealed=true
            where business_id=%s and id=%s and revision=%s""",
            (business, identity_quote, number),
        )
        await connection.execute(
            "update collective.proposals set status='confirmed' where id=%s", (identity,)
        )
        row["status"] = "confirmed"
        await event(connection, subject, access, row, "quote_confirmed")
        await set_context(connection, "coordinator", subject)
        as_of = clock.now()
        active = await (
            await connection.execute(
                "select * from collective.pools where spec_id=%s and state<>'released' order by id",
                (spec,),
            )
        ).fetchall()
        for pool in active:
            await recalculate(connection, pool, as_of)
        for approval in approvals:
            if approval["pool_id"] is None and approval["state"] == "valid":
                need = await needs.load_need(
                    connection, approval["business_id"], approval["need_id"]
                )
                evaluation = await solo.evaluate(connection, need, approval["quote_id"], as_of)
                await solo.revalidate(connection, approval, evaluation, as_of)
        result = await view(connection, row, revision, access)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
