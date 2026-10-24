from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID

from pydantic import Field

from collective.domain.approvals import BoundTerms
from collective.domain.costs import CostBreakdown
from collective.domain.money import round_money
from collective.domain.types import (
    BaseUnit,
    DecimalValue,
    Money,
    NonNegative,
    Positive,
    Rate,
    Revision,
    SignedMoney,
    Snapshot,
)


class InvoiceLine(Snapshot):
    line: int = Field(ge=0)
    quantity: Positive | None
    base_unit: BaseUnit | None
    unit_price: NonNegative | None
    line_total: Money | None
    spec_id: UUID | None
    spec_revision: Revision | None
    mapping_confirmed: bool


class InvoiceFee(Snapshot):
    label: str | None
    amount: Money | None


class ConfirmedInvoice(Snapshot):
    id: UUID
    business_id: UUID
    revision: Revision
    supplier_id: UUID
    confirmed: bool
    purpose: Literal["historical", "delivered"]
    currency: str | None
    goods_total: Money | None
    delivery_fee: Money | None
    tax_amount: Money | None
    discount: Money | None
    surcharge: Money | None
    total: Money | None
    lines: tuple[InvoiceLine, ...]
    additional_fees: tuple[InvoiceFee, ...] = ()
    tax_rate: Rate | None = None


class IssuedInvoiceOrder(Snapshot):
    id: UUID
    business_id: UUID
    amount: Money
    terms: BoundTerms
    cost: CostBreakdown


class InvoiceTolerances(Snapshot):
    unit_price: NonNegative = Decimal("0.01")
    amount: Money = Decimal("0.01")


class InvoiceDifference(Snapshot):
    field_path: str
    kind: Literal["unit_price", "quantity", "line_total", "fee", "total", "arithmetic"]
    agreed: DecimalValue | SignedMoney
    invoiced: DecimalValue | SignedMoney
    delta: DecimalValue | SignedMoney
    amount_difference: SignedMoney | None = None
    material: bool


class InvoiceCheckResult(Snapshot):
    status: Literal["unmatched", "passed", "flagged"]
    differences: tuple[InvoiceDifference, ...]
    missing_fields: tuple[str, ...]
    failure_codes: tuple[str, ...]
    total_variance: SignedMoney | None
    complete: bool
    tolerances: InvoiceTolerances
    version: Literal["invoice-v1"] = "invoice-v1"


def check_invoice(invoice, order, tolerances=None):
    tolerances = tolerances or InvoiceTolerances()
    differences, missing, failures = [], [], []

    def finish(matched):
        complete = matched and not missing and not failures
        return InvoiceCheckResult(
            status=(
                "flagged"
                if any(d.material for d in differences)
                else "passed"
                if complete
                else "unmatched"
            )
            if matched
            else "unmatched",
            differences=tuple(differences),
            missing_fields=tuple(dict.fromkeys(missing)),
            failure_codes=tuple(dict.fromkeys(failures)),
            total_variance=round_money(invoice.total - order.amount)
            if order is not None
            and invoice.total is not None
            and invoice.currency == "MYR"
            and matched
            else None,
            complete=complete,
            tolerances=tolerances,
        )

    def compare(path, kind, agreed, observed, *, effect=None, new_fee=False):
        if observed is None:
            missing.append(path)
            return
        if agreed == observed:
            return
        delta = observed - agreed
        amount = (
            effect
            if effect is not None
            else (round_money(delta) if kind not in {"unit_price", "quantity"} else None)
        )
        material = (
            (new_fee and observed > 0)
            or kind == "quantity"
            or (
                abs(delta) > tolerances.unit_price
                if kind == "unit_price"
                else amount is not None and abs(amount) >= tolerances.amount
            )
        )
        differences.append(
            InvoiceDifference(
                field_path=path,
                kind=kind,
                agreed=agreed,
                invoiced=observed,
                delta=delta,
                amount_difference=amount,
                material=material,
            )
        )

    if not invoice.confirmed or invoice.purpose != "delivered":
        failures.append("CONFIRMED_DELIVERED_INVOICE_REQUIRED")
        return finish(False)
    if order is None:
        failures.append("NO_ISSUED_PO")
        return finish(False)
    terms, cost = order.terms, order.cost
    if invoice.business_id != order.business_id or invoice.supplier_id != terms.supplier_id:
        failures.append("PO_CONTEXT_MISMATCH")
        return finish(False)
    if invoice.currency != "MYR":
        failures.append("UNSUPPORTED_CURRENCY")
        return finish(False)
    if cost.agreed_cost != order.amount or any(
        getattr(cost, field) is None
        for field in (
            "unit_price",
            "goods",
            "delivery_fee",
            "tax",
            "discount",
            "surcharge",
        )
    ):
        failures.append("ISSUED_TERMS_INCOMPLETE")
        return finish(False)
    matched = bool(invoice.lines)
    if not matched:
        failures.append("NO_INVOICE_LINES")
    with localcontext() as context:
        context.prec = 40
        try:
            for line in invoice.lines:
                path = f"lines.{line.line}"
                comparable = line.mapping_confirmed and (
                    line.spec_id,
                    line.spec_revision,
                    line.base_unit,
                ) == (terms.spec_id, terms.spec_revision, terms.base_unit)
                if not comparable or line.quantity is None or line.unit_price is None:
                    matched = False
                    failures.append("UNRESOLVED_LINE_MAPPING")
                    missing.append(path)
                    continue
                compare(
                    path + ".unit_price",
                    "unit_price",
                    cost.unit_price,
                    line.unit_price,
                    effect=round_money(line.quantity * (line.unit_price - cost.unit_price)),
                )
                compare(
                    path + ".line_total",
                    "line_total",
                    round_money(line.quantity * cost.unit_price),
                    line.line_total,
                )
                compare(
                    path + ".arithmetic",
                    "arithmetic",
                    round_money(line.quantity * line.unit_price),
                    line.line_total,
                )
            if matched:
                quantity = sum((line.quantity for line in invoice.lines), Decimal(0))
                compare(
                    "quantity",
                    "quantity",
                    terms.quantity,
                    quantity,
                    effect=round_money((quantity - terms.quantity) * cost.unit_price),
                )
            for field, agreed in (
                ("goods_total", cost.goods),
                ("delivery_fee", cost.delivery_fee),
                ("tax_amount", cost.tax),
                ("discount", cost.discount),
                ("surcharge", cost.surcharge),
                ("total", order.amount),
            ):
                observed = getattr(invoice, field)
                compare(
                    field,
                    "total" if field in {"goods_total", "total"} else "fee",
                    agreed,
                    observed,
                    effect=round_money(agreed - observed)
                    if field == "discount" and observed is not None
                    else None,
                    new_fee=field in {"delivery_fee", "surcharge", "tax_amount"} and agreed == 0,
                )
            for index, fee in enumerate(invoice.additional_fees):
                compare(f"additional_fees.{index}", "fee", Decimal(0), fee.amount, new_fee=True)
                if not fee.label:
                    missing.append(f"additional_fees.{index}.label")
            if all(line.line_total is not None for line in invoice.lines) and invoice.lines:
                compare(
                    "goods_total.arithmetic",
                    "arithmetic",
                    round_money(sum((line.line_total for line in invoice.lines), Decimal(0))),
                    invoice.goods_total,
                )
            components = (
                invoice.goods_total,
                invoice.delivery_fee,
                invoice.tax_amount,
                invoice.discount,
                invoice.surcharge,
            )
            if all(v is not None for v in components) and all(
                fee.amount is not None for fee in invoice.additional_fees
            ):
                expected = round_money(
                    invoice.goods_total
                    + invoice.delivery_fee
                    + invoice.tax_amount
                    - invoice.discount
                    + invoice.surcharge
                    + sum((fee.amount for fee in invoice.additional_fees), Decimal(0))
                )
                compare("total.arithmetic", "arithmetic", expected, invoice.total)
                if invoice.tax_rate is not None and not invoice.additional_fees:
                    subtotal = (
                        invoice.goods_total
                        - invoice.discount
                        + invoice.delivery_fee
                        + invoice.surcharge
                    )
                    compare(
                        "tax_amount.arithmetic",
                        "arithmetic",
                        round_money(subtotal * invoice.tax_rate),
                        invoice.tax_amount,
                    )
        except ValueError:
            failures.append("UNSUPPORTED_ARITHMETIC_RANGE")
            matched = False
    return finish(matched)
