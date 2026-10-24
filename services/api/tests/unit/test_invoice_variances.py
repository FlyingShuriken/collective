from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from collective.domain.invoices import (
    ConfirmedInvoice,
    InvoiceLine,
    InvoiceTolerances,
    IssuedInvoiceOrder,
    check_invoice,
)


def fixtures():
    business, supplier, spec = uuid4(), uuid4(), uuid4()
    order = IssuedInvoiceOrder.model_validate(
        {
            "id": str(uuid4()),
            "business_id": str(business),
            "amount": "285.00",
            "terms": {
                "need_id": str(uuid4()),
                "need_revision": 1,
                "supplier_id": str(supplier),
                "spec_id": str(spec),
                "spec_revision": 1,
                "quantity": "1000",
                "base_unit": "piece",
                "delivery_mode": "per_business",
                "delivery_location": None,
                "delivery_window": {"start": "2026-10-24T00:00:00Z", "end": "2026-10-25T00:00:00Z"},
                "credit_days": 30,
                "conditions_fingerprint": "synthetic",
            },
            "cost": {
                "quote_id": str(uuid4()),
                "quote_revision": 2,
                "unit_price": "0.270",
                "tier_min_quantity": "2000",
                "goods": "270.00",
                "delivery_fee": "15.00",
                "tax": "0.00",
                "discount": "0.00",
                "surcharge": "0.00",
                "agreed_cost": "285.00",
                "expected_cost": "300.00",
                "decision_cost": "300.00",
            },
        }
    )
    invoice = ConfirmedInvoice(
        id=uuid4(),
        business_id=business,
        revision=1,
        supplier_id=supplier,
        confirmed=True,
        purpose="delivered",
        currency="MYR",
        goods_total="275.00",
        delivery_fee="15.00",
        tax_amount="0.00",
        discount="0.00",
        surcharge="0.00",
        total="290.00",
        lines=(
            InvoiceLine(
                line=0,
                quantity="1000",
                base_unit="piece",
                unit_price="0.275",
                line_total="275.00",
                spec_id=spec,
                spec_revision=1,
                mapping_confirmed=True,
            ),
        ),
    )
    return invoice, order


def test_final_issued_tier_gives_rm5_variance_despite_subcent_unit_difference():
    invoice, order = fixtures()
    result = check_invoice(invoice, order)
    assert result.status == "flagged" and result.complete
    assert result.total_variance == Decimal("5.00")
    price = next(d for d in result.differences if d.kind == "unit_price")
    assert price.delta == Decimal("0.005") and price.amount_difference == Decimal("5.00")
    assert not price.material
    assert next(d for d in result.differences if d.kind == "line_total").material
    assert invoice.lines[0].unit_price == Decimal("0.275") and order.cost.unit_price == Decimal(
        "0.270"
    )


def test_exact_invoice_passes_against_agreement_not_fee_reserve():
    invoice, order = fixtures()
    invoice = invoice.model_copy(
        update={
            "goods_total": Decimal("270"),
            "total": Decimal("285"),
            "lines": (
                invoice.lines[0].model_copy(
                    update={"unit_price": Decimal("0.270"), "line_total": Decimal("270")}
                ),
            ),
        }
    )
    result = check_invoice(invoice, order)
    assert result.status == "passed" and not result.differences
    assert result.total_variance == 0 and order.cost.decision_cost == Decimal("300")


def test_observed_fee_reserve_cannot_hide_delivery_charged_against_a_zero_fee_agreement():
    invoice, order = fixtures()
    order = IssuedInvoiceOrder.model_validate(
        order.model_dump()
        | {
            "amount": "270.00",
            "cost": order.cost.model_dump()
            | {
                "delivery_fee": "0.00",
                "agreed_cost": "270.00",
                "expected_cost": "285.00",
                "decision_cost": "285.00",
            },
        }
    )
    invoice = ConfirmedInvoice.model_validate(
        invoice.model_dump()
        | {
            "goods_total": "270.00",
            "total": "285.00",
            "lines": [
                invoice.lines[0].model_dump()
                | {
                    "unit_price": "0.270",
                    "line_total": "270.00",
                }
            ],
        }
    )
    result = check_invoice(invoice, order)
    fee = next(d for d in result.differences if d.field_path == "delivery_fee")
    assert fee.agreed == 0 and fee.invoiced == 15 and fee.material
    assert result.total_variance == Decimal("15.00") and result.status == "flagged"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"currency": "USD"}, "UNSUPPORTED_CURRENCY"),
        ({"confirmed": False}, "CONFIRMED_DELIVERED_INVOICE_REQUIRED"),
        ({"purpose": "historical"}, "CONFIRMED_DELIVERED_INVOICE_REQUIRED"),
        ({"supplier_id": uuid4()}, "PO_CONTEXT_MISMATCH"),
        ({"business_id": uuid4()}, "PO_CONTEXT_MISMATCH"),
        ({"lines": ()}, "NO_INVOICE_LINES"),
    ],
)
def test_unusable_context_is_unmatched_never_passed(change, code):
    invoice, order = fixtures()
    result = check_invoice(invoice.model_copy(update=change), order)
    assert result.status == "unmatched" and code in result.failure_codes
    assert result.total_variance is None


def test_no_issued_po_is_an_unmatched_invoice():
    invoice, _ = fixtures()
    assert check_invoice(invoice, None).failure_codes == ("NO_ISSUED_PO",)


@pytest.mark.parametrize(
    "change",
    [
        {"mapping_confirmed": False},
        {"spec_id": uuid4()},
        {"spec_revision": 2},
        {"base_unit": "ml"},
        {"unit_price": None},
        {"quantity": None},
    ],
)
def test_partial_or_unknown_mapping_preserves_known_differences_without_passing(change):
    invoice, order = fixtures()
    partial = invoice.lines[0].model_copy(update=change | {"line": 1})
    result = check_invoice(invoice.model_copy(update={"lines": (invoice.lines[0], partial)}), order)
    assert result.status == "unmatched" and "UNRESOLVED_LINE_MAPPING" in result.failure_codes
    assert result.differences and not result.complete


def test_unknown_charges_remain_unknown_but_known_variances_are_flagged():
    invoice, order = fixtures()
    result = check_invoice(invoice.model_copy(update={"discount": None, "total": None}), order)
    assert result.status == "flagged" and result.total_variance is None and not result.complete
    assert set(result.missing_fields) == {"discount", "total"}
    assert not any(d.field_path == "discount" for d in result.differences)


def test_new_fee_is_highlighted_even_with_a_large_configured_amount_tolerance():
    invoice, order = fixtures()
    invoice = ConfirmedInvoice.model_validate(
        invoice.model_dump()
        | {
            "additional_fees": [{"label": "unagreed handling", "amount": "0.01"}],
            "total": "290.01",
        }
    )
    result = check_invoice(invoice, order, InvoiceTolerances(amount="100.00"))
    assert next(d for d in result.differences if d.field_path == "additional_fees.0").material


def test_quantity_and_line_price_effects_are_separate_not_added_to_total_variance():
    invoice, order = fixtures()
    invoice = ConfirmedInvoice.model_validate(
        invoice.model_dump()
        | {
            "goods_total": "220.00",
            "total": "235.00",
            "lines": [invoice.lines[0].model_dump() | {"quantity": "800", "line_total": "220.00"}],
        }
    )
    result = check_invoice(invoice, order)
    quantity = next(d for d in result.differences if d.kind == "quantity")
    assert quantity.delta == -200 and quantity.amount_difference == Decimal("-54.00")
    assert quantity.material and result.total_variance == Decimal("-50.00")


def test_each_duplicate_item_line_is_compared_and_quantities_aggregate():
    invoice, order = fixtures()
    lines = tuple(
        invoice.lines[0].model_copy(
            update={"line": i, "quantity": Decimal("500"), "line_total": Decimal("137.50")}
        )
        for i in range(2)
    )
    result = check_invoice(invoice.model_copy(update={"lines": lines}), order)
    assert not any(d.kind == "quantity" for d in result.differences)
    assert len([d for d in result.differences if d.kind == "line_total"]) == 2


def test_below_threshold_unit_changes_are_retained_and_strict_unit_boundary_is_preserved():
    invoice, order = fixtures()
    result = check_invoice(invoice, order, InvoiceTolerances(unit_price="0.005", amount="10.00"))
    assert result.status == "passed" and result.differences
    assert not any(d.material for d in result.differences)
    result = check_invoice(invoice, order, InvoiceTolerances(unit_price="0.004", amount="10.00"))
    assert next(d for d in result.differences if d.kind == "unit_price").material


def test_bad_arithmetic_and_unknown_additional_fee_do_not_turn_into_a_clean_check():
    invoice, order = fixtures()
    invoice = ConfirmedInvoice.model_validate(
        invoice.model_dump()
        | {
            "total": "1.00",
            "additional_fees": [{"label": None, "amount": None}],
        }
    )
    result = check_invoice(invoice, order)
    assert not result.complete and set(result.missing_fields) == {
        "additional_fees.0",
        "additional_fees.0.label",
    }
    assert next(d for d in result.differences if d.field_path == "total").delta == Decimal(
        "-284.00"
    )


def test_overflow_fails_closed_and_binary_floats_are_not_valid_money():
    invoice, order = fixtures()
    invoice = ConfirmedInvoice.model_validate(
        invoice.model_dump()
        | {
            "lines": [
                invoice.lines[0].model_dump()
                | {"quantity": "999999999999", "unit_price": "999999999999"}
            ],
        }
    )
    result = check_invoice(invoice, order)
    assert result.status == "unmatched" and "UNSUPPORTED_ARITHMETIC_RANGE" in result.failure_codes
    with pytest.raises(ValidationError):
        InvoiceTolerances(amount=0.01)


def test_discount_cash_effect_is_signed_oppositely_to_a_charge():
    invoice, order = fixtures()
    result = check_invoice(
        invoice.model_copy(update={"discount": Decimal("5.00"), "total": Decimal("285.00")}), order
    )
    discount = next(d for d in result.differences if d.field_path == "discount")
    assert discount.delta == Decimal("5.00") and discount.amount_difference == Decimal("-5.00")


def test_money_differences_support_the_declared_numeric_18_2_range():
    invoice, order = fixtures()
    invoice = invoice.model_copy(update={"total": Decimal("1000000000000000.00")})
    result = check_invoice(invoice, order)
    assert result.status == "flagged" and not result.failure_codes
    assert result.total_variance == Decimal("999999999999715.00")


def test_missing_charge_cannot_turn_an_otherwise_exact_match_into_passed():
    invoice, order = fixtures()
    invoice = invoice.model_copy(
        update={
            "goods_total": Decimal("270.00"),
            "total": Decimal("285.00"),
            "discount": None,
            "lines": (
                invoice.lines[0].model_copy(
                    update={"unit_price": Decimal("0.270"), "line_total": Decimal("270.00")}
                ),
            ),
        }
    )
    result = check_invoice(invoice, order)
    assert result.status == "unmatched" and result.missing_fields == ("discount",)


def test_inconsistent_printed_tax_rate_does_not_receive_a_passed_check():
    invoice, order = fixtures()
    invoice = invoice.model_copy(update={"tax_rate": Decimal("0.06")})
    result = check_invoice(invoice, order)
    tax = next(d for d in result.differences if d.field_path == "tax_amount.arithmetic")
    assert tax.agreed == Decimal("17.40") and tax.invoiced == 0 and tax.material
