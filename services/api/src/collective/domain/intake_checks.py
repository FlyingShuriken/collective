"""Checks on extracted values; never repair a reading or infer an absent charge."""

import re
from decimal import Decimal, localcontext

from collective.domain.costs import calculate_payable
from collective.domain.money import round_money
from collective.domain.types import Money, Snapshot


class IntakeChecks(Snapshot):
    flags: tuple[str, ...]
    calculated_goods: Money | None = None
    calculated_total: Money | None = None


def ambiguous_date(literal):
    if literal is None:
        return False
    parts = re.fullmatch(r"\s*(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?\s*", literal)
    if parts:
        first, second = int(parts[1]), int(parts[2])
        return parts[3] is None or (first != second and first <= 12 and second <= 12)
    return re.search(r"(?<!\d)\d{4}(?!\d)", literal) is None


def check_reading(reading):
    flags = [f"{issue.field_path}:{issue.reason}" for issue in reading.issues]
    if reading.confidence_label != "high":
        flags.append("reading:review")
    if any(anchor.confidence_label != "high" for anchor in reading.evidence):
        flags.append("fields:review")
    if reading.kind == "supplier_reply":
        return IntakeChecks(flags=tuple(flags + ["source:needs_quote_context"]))
    if reading.kind == "unreadable":
        return IntakeChecks(flags=tuple(flags + ["source:unreadable"]))
    if reading.kind == "stock":
        for name in ("stock_description", "stock_quantity", "stock_unit", "stock_observed_at"):
            if getattr(reading, name) is None:
                flags.append(f"{name}:missing")
        if ambiguous_date(reading.stock_date_text):
            flags.append("stock_observed_at:ambiguous")
        return IntakeChecks(flags=tuple(dict.fromkeys(flags)))

    required = (
        "supplier_name",
        "document_date",
        "currency",
        "goods_total",
        "delivery_fee",
        "tax_amount",
        "tax_rate",
        "discount",
        "surcharge",
        "total",
        "credit_days",
        "delivery_mode",
    )
    for name in required:
        if getattr(reading, name) is None:
            flags.append(f"{name}:missing")
    if ambiguous_date(reading.date_text):
        flags.append("document_date:ambiguous")
    if reading.currency not in {None, "MYR"}:
        flags.append("currency:unsupported")
    if not reading.lines:
        flags.append("lines:missing")
    goods, total = None, None
    with localcontext() as context:
        context.prec = 40
        for index, line in enumerate(reading.lines):
            for name in ("description", "quantity", "unit", "unit_price", "line_total"):
                if getattr(line, name) is None:
                    flags.append(f"lines.{index}.{name}:missing")
            if line.quantity is not None and line.unit_price is not None:
                try:
                    expected = round_money(line.quantity * line.unit_price)
                    if line.line_total is not None and expected != line.line_total:
                        flags.append(f"lines.{index}.total:mismatch")
                except ValueError:
                    flags.append(f"lines.{index}.amount:overflow")
        try:
            if reading.lines and all(line.line_total is not None for line in reading.lines):
                goods = round_money(sum((line.line_total for line in reading.lines), Decimal(0)))
                if reading.goods_total is not None and reading.goods_total != goods:
                    flags.append("goods_total:mismatch")
            for index, fee in enumerate(reading.additional_fees):
                if fee.label is None:
                    flags.append(f"additional_fees.{index}.label:missing")
                if fee.amount is None:
                    flags.append(f"additional_fees.{index}.amount:missing")
            if any(fee.amount for fee in reading.additional_fees):
                flags.append("additional_fees:comparison_basis_unknown")
            components = (
                reading.goods_total,
                reading.delivery_fee,
                reading.tax_amount,
                reading.discount,
                reading.surcharge,
            )
            if all(value is not None for value in components) and all(
                fee.amount is not None for fee in reading.additional_fees
            ):
                total = round_money(
                    reading.goods_total
                    + reading.delivery_fee
                    + reading.tax_amount
                    - reading.discount
                    + reading.surcharge
                    + sum((fee.amount for fee in reading.additional_fees), Decimal(0))
                )
                if reading.total is not None and total != reading.total:
                    flags.append("total:mismatch")
                if total < 0:
                    flags.append("total:negative")
                    total = None
                if reading.tax_rate is not None and not reading.additional_fees:
                    _, tax = calculate_payable(
                        reading.goods_total,
                        reading.delivery_fee,
                        reading.discount,
                        reading.surcharge,
                        reading.tax_rate,
                    )
                    if tax != reading.tax_amount:
                        flags.append("tax_amount:mismatch")
        except ValueError:
            flags.append("arithmetic:overflow")
    return IntakeChecks(
        flags=tuple(dict.fromkeys(flags)), calculated_goods=goods, calculated_total=total
    )
