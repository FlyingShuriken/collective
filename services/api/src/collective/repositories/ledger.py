from datetime import datetime
from decimal import Decimal
from uuid import UUID

from psycopg import AsyncConnection, sql
from pydantic import ValidationError

from collective.api.errors import DomainError
from collective.domain.approvals import BoundTerms
from collective.domain.baselines import HistoricalBasis, historical_cost
from collective.domain.replenishment import StockMovement, StockSnapshot
from collective.domain.types import NeedSnapshot


async def historical_basis(
    connection: AsyncConnection, need: NeedSnapshot
) -> HistoricalBasis | None:
    rows = await (
        await connection.execute(
            """
        select p.*,l.unit_price,l.base_unit,l.spec_id,l.spec_revision,b.zone
        from (select distinct on (id) * from collective.purchases
              where business_id=%s and sealed order by id,revision desc) p
        join collective.purchase_lines l
          on (l.business_id,l.purchase_id,l.purchase_revision)=(p.business_id,p.id,p.revision)
        join collective.businesses b on b.id=p.business_id
        where l.spec_id=%s and l.spec_revision=%s
          and (select count(*) from collective.purchase_lines x where
               (x.business_id,x.purchase_id,x.purchase_revision)=(p.business_id,p.id,p.revision))=1
        order by p.document_date desc nulls last,p.created_at desc,p.id
    """,
            (need.business_id, need.spec_id, need.spec_revision),
        )
    ).fetchall()
    fields = set(HistoricalBasis.model_fields) - {"confirmed", "fee_basis"}
    for row in rows:
        try:
            basis = HistoricalBasis(
                **{k: row[k] for k in fields}, confirmed=True, fee_basis="per_order"
            )
        except ValidationError:
            continue
        if historical_cost(need, basis) is not None:
            return basis
    return None


async def current_stock(
    connection: AsyncConnection, business: UUID, spec: UUID, revision: int, as_of: datetime
) -> StockSnapshot | None:
    row = await (
        await connection.execute(
            """select * from collective.stock_observations
        where business_id=%s and spec_id=%s and spec_revision=%s and kind='stock'
        and observed_at<=%s order by observed_at desc,created_at desc,id desc limit 1""",
            (business, spec, revision, as_of),
        )
    ).fetchone()
    if row is None:
        return None
    movements = []
    changes = await (
        await connection.execute(
            """select * from collective.stock_observations
        where business_id=%s and spec_id=%s and spec_revision=%s and kind<>'stock'
        and observed_at>%s and observed_at<=%s order by observed_at,id""",
            (business, spec, revision, row["observed_at"], as_of),
        )
    ).fetchall()
    for change in changes:
        if change["quantity"] > 0:
            movements.append(
                StockMovement(
                    id=change["id"],
                    quantity=change["quantity"],
                    at=change["observed_at"],
                    kind="waste" if change["kind"] == "waste" else "received",
                    confirmed=True,
                )
            )
    orders = await (
        await connection.execute(
            """select po.*,
        coalesce((select sum(s.quantity) from collective.stock_observations s
                  where s.business_id=po.business_id and s.po_id=po.id
                    and s.observed_at<=%s),0) as received
        from collective.purchase_orders po
        where po.business_id=%s and po.spec_id=%s and po.spec_revision=%s and po.issued_at<=%s""",
            (as_of, business, spec, revision, as_of),
        )
    ).fetchall()
    for order in orders:
        remaining = max(Decimal(0), order["quantity"] - order["received"])
        if not remaining:
            continue
        try:
            terms = BoundTerms.model_validate(order["snapshot"].get("terms"))
        except ValidationError:
            return None
        arrival = terms.delivery_window.end
        if arrival <= as_of:
            return None
        movements.append(
            StockMovement(
                id=order["id"], quantity=remaining, at=arrival, kind="incoming", confirmed=True
            )
        )
    return StockSnapshot(
        quantity=row["quantity"], observed_at=row["observed_at"], movements=tuple(movements)
    )


async def ledger_page(
    connection: AsyncConnection,
    business: UUID,
    kind: str,
    cursor: UUID | None,
    limit: int,
    visible: bool,
) -> dict:
    table, timestamp = {
        "purchase": ("purchases", "created_at"),
        "quote": ("quote_revisions", "created_at"),
        "stock": ("stock_observations", "created_at"),
        "po": ("purchase_orders", "issued_at"),
    }[kind]
    source = sql.SQL("select * from collective.{} where business_id=%s").format(
        sql.Identifier(table)
    )
    if kind in {"purchase", "quote"}:
        source = sql.SQL(
            """select distinct on (id) * from collective.{}
            where business_id=%s and sealed order by id,revision desc"""
        ).format(sql.Identifier(table))
    query = sql.SQL("with current_records as ({}) select * from current_records").format(source)
    parameters = [business]
    if cursor:
        previous = await (
            await connection.execute(
                sql.SQL(
                    "with current_records as ({}) select * from current_records where id=%s"
                ).format(source),
                (business, cursor),
            )
        ).fetchone()
        if previous is None:
            raise DomainError("NOT_FOUND", "Resource not found.", 404)
        query += sql.SQL(" where ({},id)<(%s,%s)").format(sql.Identifier(timestamp))
        parameters.extend((previous[timestamp], previous["id"]))
    query += sql.SQL(" order by {} desc,id desc limit %s").format(sql.Identifier(timestamp))
    parameters.append(limit + 1)
    rows = await (await connection.execute(query, parameters)).fetchall()
    items = []
    for row in rows[:limit]:
        item = {
            "id": row["id"],
            "kind": kind,
            "revision": row.get("revision", 1),
            "recorded_at": row[timestamp],
            "quantity": row.get("quantity"),
            "price_visibility": "visible" if visible else "hidden",
        }
        if visible:
            item |= {
                "total": row.get("total", row.get("amount")),
                "source_id": row.get("source_id"),
                "supplier_id": row.get("supplier_id"),
            }
        items.append(item)
    return {"items": items, "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}
