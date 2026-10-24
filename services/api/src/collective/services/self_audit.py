from decimal import Decimal

from pydantic import ValidationError

from collective.api.dto import HiddenSelfAudit, VisibleSelfAudit
from collective.domain.invoices import InvoiceCheckResult


async def project(connection, business, access, as_of):
    purchases = await (
        await connection.execute(
            """select distinct on (id) id,revision,purpose,created_at from collective.purchases
        where business_id=%s and sealed and created_at<=%s order by id,revision desc""",
            (business, as_of),
        )
    ).fetchall()
    checks = await (
        await connection.execute(
            """select * from collective.invoice_checks where business_id=%s and created_at<=%s
        order by created_at desc,id desc""",
            (business, as_of),
        )
    ).fetchall()
    current = {}
    for row in checks:
        current.setdefault((row["purchase_id"], row["purchase_revision"]), row)
    counts = dict(
        delivered_records=0,
        checked_records=0,
        mismatched_records=0,
        unmatched_records=0,
        incomplete_checks=0,
        confirmed_variances=0,
        documented_recoveries=0,
    )
    variance, recovery = Decimal(0), Decimal(0)
    for purchase in purchases:
        if purchase["purpose"] != "delivered":
            continue
        counts["delivered_records"] += 1
        row = current.get((purchase["id"], purchase["revision"]))
        if row is None or row["po_id"] is None:
            counts["unmatched_records"] += 1
            continue
        try:
            checked = InvoiceCheckResult.model_validate(row["result"])
        except ValidationError:
            counts["incomplete_checks"] += 1
            continue
        if row["review_status"] == "confirmed" and row["confirmed_amount"] is not None:
            if row["confirmed_amount"] > 0:
                counts["confirmed_variances"] += 1
                variance += row["confirmed_amount"]
            if row["recovery_amount"] is not None and row["recovery_source_id"] is not None:
                counts["documented_recoveries"] += 1
                recovery += row["recovery_amount"]
        if (
            not checked.complete
            or checked.status != row["status"]
            or checked.status not in {"passed", "flagged"}
        ):
            counts["incomplete_checks"] += 1
            continue
        counts["checked_records"] += 1
        counts["mismatched_records"] += int(any(d.material for d in checked.differences))
    recorded = [row["created_at"] for row in purchases]
    summary = dict(
        period_start=min(recorded) if recorded else None,
        period_end=max(recorded) if recorded else None,
        purchase_records=len(purchases),
        **counts,
    )
    if not access.can_view_prices:
        return HiddenSelfAudit(**summary)
    return VisibleSelfAudit(
        **summary, confirmed_variance_amount=variance, documented_recovery_amount=recovery
    )
