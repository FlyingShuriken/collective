from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from pydantic import ValidationError

from collective.domain.costs import Quote, Tier


@dataclass(frozen=True)
class QuoteRecord:
    row: dict
    tiers: tuple[Tier, ...]
    quote: Quote | None
    missing_fields: tuple[str, ...]


async def confirmed_quotes(
    connection: AsyncConnection, business: UUID, spec: UUID
) -> list[QuoteRecord]:
    rows = await (
        await connection.execute(
            """
        select q.*,s.name as supplier_name from (
          select distinct on (id) * from collective.quote_revisions
          where business_id=%s and sealed order by id,revision desc
        ) q join collective.suppliers s on s.id=q.supplier_id
        where q.spec_id=%s order by s.name,q.id
    """,
            (business, spec),
        )
    ).fetchall()
    result = []
    for row in rows:
        tiers = tuple(
            Tier.model_validate(r)
            for r in await (
                await connection.execute(
                    """select min_quantity,unit_price from collective.quote_tiers
            where business_id=%s and quote_id=%s and quote_revision=%s order by min_quantity""",
                    (business, row["id"], row["revision"]),
                )
            ).fetchall()
        )
        fields = set(Quote.model_fields) - {"confirmed", "tiers", "delivery_window"}
        nullable = {
            "delivery_location",
            "free_delivery_threshold",
            "waive_split_drop_fees",
            "pooled_buyers_permitted",
        }
        missing = tuple(sorted(k for k in fields - nullable if row[k] is None))
        missing += tuple(k for k in ("arrival_start", "arrival_end") if row[k] is None)
        if not tiers:
            missing += ("tiers",)
        quote = None
        if not missing:
            try:
                quote = Quote(
                    **{k: row[k] for k in fields},
                    tiers=tiers,
                    confirmed=row["sealed"],
                    delivery_window={"start": row["arrival_start"], "end": row["arrival_end"]},
                )
            except ValidationError:
                missing = ("invalid_terms",)
        result.append(QuoteRecord(row, tiers, quote, missing))
    return result
