from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from collective.api.dto import ConfirmMapping
from collective.api.errors import DomainError
from collective.domain.units import PackMapping, SourceLine, normalize_line
from collective.repositories.db import set_context
from collective.services import mappings


@pytest.fixture
async def pending(db, tenants):
    ids = {key: uuid4() for key in ("supplier", "spec", "source", "mapping")}
    await db.execute(
        "insert into collective.suppliers(id,name) values (%s,'Mapping supplier')",
        (ids["supplier"],),
    )
    await db.execute(
        "insert into collective.business_suppliers(business_id,supplier_id) values (%s,%s)",
        (tenants["a"], ids["supplier"]),
    )
    await db.execute(
        "insert into collective.canonical_specs(id,name) values (%s,'250ml cup')", (ids["spec"],)
    )
    await db.execute(
        """insert into collective.spec_revisions
        (spec_id,revision,base_unit,pack_multiple,perishable,attributes)
        values (%s,1,'piece',1,false,%s)""",
        (ids["spec"], Jsonb({"capacity_ml": "250", "lid_diameter_mm": "80", "material": "paper"})),
    )
    await db.execute(
        """insert into collective.sources(id,business_id,kind,status,sha256,text_content,provenance)
        values (%s,%s,'text','available',%s,'250ml paper cups 80mm lid box 100','synthetic')""",
        (ids["source"], tenants["a"], ids["source"].hex * 2),
    )
    await db.execute(
        """insert into collective.sku_mappings
        (business_id,id,revision,supplier_id,supplier_sku,source_name,spec_id,spec_revision,
         pack_factors,source_id,source_unit,match_snapshot,provenance)
        values (%s,%s,1,%s,'CUP-250','250ml cups',%s,1,'["100"]',%s,'box',%s,'agent')""",
        (
            tenants["a"],
            ids["mapping"],
            ids["supplier"],
            ids["spec"],
            ids["source"],
            Jsonb({"reading": {"confidence_label": "high"}, "flags": []}),
        ),
    )
    return ids


def command(ids, **changes):
    return ConfirmMapping.model_validate(
        {
            "expected_revision": 1,
            "spec_id": ids["spec"],
            "spec_revision": 1,
            "source_unit": "box",
            "pack_factors": ["100"],
            "acknowledge_flags": True,
        }
        | changes
    )


@pytest.mark.parametrize("actor,code", [("staff", "OWNER_REQUIRED"), ("owner_b", "NOT_FOUND")])
async def test_high_confidence_mapping_cannot_be_confirmed_by_staff_or_other_owner(
    db, tenants, pending, actor, code
):
    await set_context(db, "tenant", tenants[actor])
    with pytest.raises(DomainError) as error:
        await mappings.confirm(
            db,
            tenants[actor],
            tenants["a"],
            pending["mapping"],
            command(pending),
            str(uuid4()),
            datetime.now(UTC),
        )
    assert error.value.code == code


async def test_owner_confirmation_is_new_immutable_revision_and_idempotent(db, tenants, pending):
    await set_context(db, "tenant", tenants["owner_a"])
    now, key = datetime.now(UTC), str(uuid4())
    result = await mappings.confirm(
        db, tenants["owner_a"], tenants["a"], pending["mapping"], command(pending), key, now
    )
    assert result.confirmed and result.revision == 2 and result.source_unit == "box"
    repeat = await mappings.confirm(
        db, tenants["owner_a"], tenants["a"], pending["mapping"], command(pending), key, now
    )
    assert repeat == result
    rows = await (
        await db.execute(
            "select revision,confirmed from collective.sku_mappings where id=%s order by revision",
            (pending["mapping"],),
        )
    ).fetchall()
    assert rows == [{"revision": 1, "confirmed": False}, {"revision": 2, "confirmed": True}]
    with pytest.raises(DomainError) as error:
        await mappings.confirm(
            db,
            tenants["owner_a"],
            tenants["a"],
            pending["mapping"],
            command(pending),
            str(uuid4()),
            now,
        )
    assert error.value.code == "STALE_REVISION"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"acknowledge_flags": False}, "REVIEW_REQUIRED"),
        ({"pack_factors": ["0.5"]}, "INVALID_PACK"),
        ({"source_unit": "piece", "pack_factors": ["100"]}, "INVALID_PACK"),
        ({"spec_revision": 999}, "UNSUPPORTED_SPEC"),
    ],
)
async def test_owner_must_review_attributes_and_valid_pack_bounds(
    db, tenants, pending, changes, code
):
    await set_context(db, "tenant", tenants["owner_a"])
    with pytest.raises(DomainError) as error:
        await mappings.confirm(
            db,
            tenants["owner_a"],
            tenants["a"],
            pending["mapping"],
            command(pending, **changes),
            str(uuid4()),
            datetime.now(UTC),
        )
    assert error.value.code == code


async def test_database_also_rejects_staff_human_mapping_insert(db, tenants, pending):
    await set_context(db, "tenant", tenants["staff"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.transaction():
            await db.execute(
                """insert into collective.sku_mappings
            (business_id,id,revision,supplier_id,supplier_sku,source_name,spec_id,spec_revision,
             confirmed,confirmed_by,confirmed_at,provenance)
            values (%s,%s,2,%s,'CUP-250','Cup',%s,1,true,%s,now(),'human')""",
                (
                    tenants["a"],
                    pending["mapping"],
                    pending["supplier"],
                    pending["spec"],
                    tenants["staff"],
                ),
            )


def test_confirmed_box_factors_cannot_normalize_a_carton():
    mapping = PackMapping(base_unit="piece", factors=["100"], confirmed=True, source_unit="box")
    values = dict(quantity="2", base_unit="piece", unit_price="25", currency="MYR")
    assert normalize_line(SourceLine(unit="box", **values), mapping).quantity == 200
    wrong = normalize_line(SourceLine(unit="carton", **values), mapping)
    assert wrong.quantity is None and wrong.failure_codes == ("INCOMPATIBLE_PACK_UNIT",)
