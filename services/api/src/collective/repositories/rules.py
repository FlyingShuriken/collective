from collective.domain.costs import FeeRule
from collective.domain.learning import AccuracyContext, FeeContext, UsageContext
from collective.domain.replenishment import UsageInput


async def accepted(connection, business, kind):
    rows = await (
        await connection.execute(
            """select r.* from collective.learned_rules r
        where r.sealed and r.state='accepted' and r.kind=%s and
          ((r.scope='private' and r.business_id=%s) or
           (r.scope='network' and r.distinct_businesses>=5 and r.network_id=
             (select network_id from collective.businesses where id=%s)))
        order by (r.scope='private') desc,r.accepted_at desc,r.id""",
            (kind, business, business),
        )
    ).fetchall()
    # A business's explicit private rule takes precedence for an identical context.
    by_context = {}
    for row in rows:
        by_context.setdefault(row["context_key"], row)
    return tuple(by_context.values())


async def fee_rules(connection, business):
    result = []
    for row in await accepted(connection, business, "observed_fee"):
        context = FeeContext.model_validate(row["proposed_value"]["context"])
        result.append(
            FeeRule(
                id=row["id"],
                revision=row["revision"],
                accepted=True,
                sample_count=row["sample_count"],
                business_id=business,
                **context.model_dump(exclude={"currency"}),
                amount=row["proposed_value"]["value"]["amount"],
            )
        )
    return tuple(result)


async def usage_input(connection, row):
    # A stored need cannot grant itself Gate 2 acceptance.
    stated = (row["usage_input"] or {}).get("stated_daily_rate")
    for rule in await accepted(connection, row["business_id"], "usage"):
        context = UsageContext.model_validate(rule["proposed_value"]["context"])
        if (context.spec_id, context.spec_revision, context.base_unit) == (
            row["spec_id"],
            row["spec_revision"],
            row["base_unit"],
        ):
            return UsageInput(
                stated_daily_rate=stated,
                rule_accepted=True,
                rule_revision=rule["revision"],
                rule_id=rule["id"],
                rule_daily_rate=rule["proposed_value"]["value"]["daily_rate"],
            )
    return UsageInput(stated_daily_rate=stated)


async def accuracy_warnings(connection, business, supplier):
    for row in await accepted(connection, business, "quote_accuracy"):
        if AccuracyContext.model_validate(row["proposed_value"]["context"]).supplier_id == supplier:
            data = row["proposed_value"]
            return (
                {
                    "rule_id": str(row["id"]),
                    "rule_revision": row["revision"],
                    "mismatch_count": data["value"]["mismatch_count"],
                    "checked_count": data["value"]["checked_count"],
                    "period_start": data["period_start"],
                    "period_end": data["period_end"],
                    "uncertainty": data["uncertainty"],
                },
            )
    return ()
