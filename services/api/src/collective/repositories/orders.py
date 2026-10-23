from collective.api.dto import HiddenPurchaseOrder, VisiblePurchaseOrder


async def own_order(connection, business, need, *, prices=True):
    row = await (
        await connection.execute(
            """select po.*,s.name as supplier_name from collective.purchase_orders po
        join collective.suppliers s on s.id=po.supplier_id
        where po.business_id=%s and po.need_id=%s""",
            (business, need),
        )
    ).fetchone()
    if row is None:
        return None
    summary = {
        k: row[k]
        for k in HiddenPurchaseOrder.model_fields
        if k not in {"base_unit", "price_visibility"}
    }
    summary["base_unit"] = row["snapshot"]["terms"]["base_unit"]
    if not prices:
        return HiddenPurchaseOrder(**summary)
    return VisiblePurchaseOrder(
        **summary,
        supplier_id=row["supplier_id"],
        supplier_name=row["supplier_name"],
        amount=row["amount"],
        decision_amount=row["decision_amount"],
        terms=row["snapshot"]["terms"],
        cost=row["snapshot"]["cost"],
    )
