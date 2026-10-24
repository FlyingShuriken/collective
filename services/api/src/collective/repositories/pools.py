from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from collective.api.dto import PoolOffer, UnavailablePoolOffer
from collective.api.errors import DomainError
from collective.domain.approvals import bound_terms
from collective.domain.pools import PoolEvaluation
from collective.repositories.events import Event, append_event
from collective.repositories.locks import lock_cohorts


async def lock_inputs(connection: AsyncConnection, spec: UUID, *, extra_suppliers=()) -> None:
    suppliers = await (
        await connection.execute(
            "select distinct supplier_id from collective.quote_revisions where spec_id=%s", (spec,)
        )
    ).fetchall()
    await lock_cohorts(
        connection,
        {(r["supplier_id"], spec) for r in suppliers}
        | {(supplier, spec) for supplier in extra_suppliers},
    )
    await connection.execute(
        """select id from collective.pools
        where spec_id=%s and state<>'released' order by id for update""",
        (spec,),
    )
    await connection.execute(
        """select id from collective.reorder_needs
        where spec_id=%s order by id for update""",
        (spec,),
    )


async def candidate_needs(connection: AsyncConnection, spec: UUID, revision: int) -> list[dict]:
    rows = await (
        await connection.execute(
            """select n.business_id,n.id from collective.reorder_needs n
        join collective.businesses b on b.id=n.business_id
        where n.spec_id=%s and n.spec_revision=%s and b.pool_opt_in
          and n.state='selected' and n.reserved_pool_id is null
          and not exists(select 1 from collective.pool_members pm where pm.need_id=n.id
                         and pm.state in ('offered','approved','awaiting_reapproval','released'))
          and not exists(select 1 from collective.bound_approvals a
                         where a.business_id=n.business_id and a.need_id=n.id and a.state='valid')
          and not exists(select 1 from collective.purchase_orders po
                         where po.business_id=n.business_id and po.need_id=n.id)
        order by n.id""",
            (spec, revision),
        )
    ).fetchall()
    return rows


async def supplier_mapping_confirmed(
    connection: AsyncConnection, business: UUID, supplier: UUID, spec: UUID, revision: int
) -> bool:
    row = await (
        await connection.execute(
            """select exists(
        select 1 from collective.sku_mappings m where m.business_id=%s and m.supplier_id=%s
          and m.spec_id=%s and m.spec_revision=%s and m.confirmed
          and not exists(select 1 from collective.sku_mappings newer
            where newer.business_id=m.business_id and newer.supplier_id=m.supplier_id
              and newer.supplier_sku=m.supplier_sku and newer.confirmed
              and newer.revision>m.revision)) as confirmed""",
            (business, supplier, spec, revision),
        )
    ).fetchone()
    return row["confirmed"]


async def own_offer(
    connection: AsyncConnection, business: UUID, pool: UUID, as_of: datetime
) -> PoolOffer | UnavailablePoolOffer:
    row = await (
        await connection.execute(
            """select e.snapshot from collective.member_evaluations e
        where e.business_id=%s and e.pool_id=%s order by e.revision desc limit 1""",
            (business, pool),
        )
    ).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    model = (
        UnavailablePoolOffer if row["snapshot"]["price_visibility"] == "unavailable" else PoolOffer
    )
    offer = model.model_validate(row["snapshot"])
    if offer.deadline <= as_of and offer.release_state != "released":
        offer = offer.model_copy(
            update={
                "eligibility": "unavailable",
                "release_state": "ineligible",
                "failure_codes": tuple(dict.fromkeys((*offer.failure_codes, "OFFER_EXPIRED"))),
            }
        )
    return offer


async def reserve(
    connection: AsyncConnection, pool: UUID, evaluation: PoolEvaluation, checks: dict
) -> dict[UUID, PoolOffer]:
    first = evaluation.members[0]
    deadline = min(min(m.need.order_window.end, m.quote.valid_until) for m in evaluation.members)
    await connection.execute(
        """insert into collective.pools
        (id,supplier_id,spec_id,spec_revision,offer_family,delivery_mode,deadline,state)
        values (%s,%s,%s,%s,%s,%s,%s,'offered')""",
        (
            pool,
            first.quote.supplier_id,
            first.need.spec_id,
            first.need.spec_revision,
            first.quote.offer_family,
            first.quote.delivery_mode,
            deadline,
        ),
    )
    members = []
    for member in evaluation.members:
        await connection.execute(
            """insert into collective.pool_members
            (pool_id,business_id,need_id,state) values (%s,%s,%s,'offered')""",
            (pool, member.business_id, member.need_id),
        )
        updated = await connection.execute(
            """update collective.reorder_needs
            set state='reserved',reserved_pool_id=%s,revision=revision+1
            where business_id=%s and id=%s and revision=%s and state='selected'
              and reserved_pool_id is null""",
            (pool, member.business_id, member.need_id, member.need.revision),
        )
        if updated.rowcount != 1:
            raise DomainError(
                "NEED_COMMITTED", "This need changed during discovery. Refresh and review.", 409
            )
        members.append(
            member.model_copy(
                update={
                    "need": member.need.model_copy(
                        update={
                            "state": "reserved",
                            "reserved_pool_id": pool,
                            "revision": member.need.revision + 1,
                        }
                    )
                }
            )
        )
    # Reservation changes lifecycle/revision only; preserve the independently evaluated terms.
    final = evaluation.model_copy(update={"members": tuple(members)})
    internal = final.model_dump(mode="json") | {"input_checks": checks, "schema_version": "1"}
    await connection.execute(
        "insert into collective.pool_evaluations values (%s,1,%s,%s)",
        (pool, final.evaluated_at, Jsonb(internal)),
    )
    offers = {}
    for member in final.members:
        supplier = await (
            await connection.execute(
                "select name from collective.suppliers where id=%s", (member.quote.supplier_id,)
            )
        ).fetchone()
        offer = PoolOffer(
            pool_id=pool,
            need_id=member.need_id,
            need_revision=member.need.revision,
            evaluation_revision=1,
            evaluated_at=final.evaluated_at,
            deadline=deadline,
            own_quantity=member.need.quantity,
            base_unit=member.need.base_unit,
            supplier_id=member.quote.supplier_id,
            supplier_name=supplier["name"],
            quote_id=member.quote.id,
            quote_revision=member.quote.revision,
            delivery_window=member.delivery_window,
            agreed_delivered_cost=member.cost.agreed_cost,
            expected_delivered_cost=member.cost.expected_cost,
            decision_delivered_cost=member.decision_cost,
            historical_comparable_cost=member.historical_comparable_cost,
            current_solo_comparable_cost=member.current_solo_comparable_cost,
            historical_saving=member.historical_saving,
            pooling_saving=member.pooling_saving,
            credit_days=member.quote.credit_days,
            terms=bound_terms(member),
            conditions=member.quote.conditions,
            member_count=len(final.members),
            eligibility="eligible",
            failure_codes=(),
            approval_state="not_approved",
            release_state="waiting_for_approvals",
        )
        await connection.execute(
            "insert into collective.member_evaluations values (%s,%s,1,%s)",
            (pool, member.business_id, Jsonb(offer.model_dump(mode="json"))),
        )
        await append_event(
            connection,
            Event(
                business_id=member.business_id,
                actor_id=None,
                actor_kind="system",
                event_type="pool_offered",
                entity_id=pool,
                entity_revision=1,
                correlation_id=pool,
                summary="An eligible pooled option reserved your selected quantity for review.",
            ),
        )
        offers[member.business_id] = offer
    return offers
