import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from collective.repositories.db import set_context


@pytest.fixture
async def records(db, tenants):
    ids = tenants | {
        name: uuid4()
        for name in (
            "supplier",
            "spec",
            "family",
            "pool",
            "release",
            "quote_a",
            "quote_b",
            "need_a",
            "need_b",
            "approval_a",
            "approval_b",
            "source_a",
            "source_b",
            "proposal_a",
            "proposal_b",
        )
    }
    now = datetime.now(UTC)
    await db.execute(
        "insert into collective.suppliers (id,name) values (%s,'Test supplier')", (ids["supplier"],)
    )
    await db.execute(
        "insert into collective.canonical_specs (id,name) values (%s,'Test cups')", (ids["spec"],)
    )
    await db.execute(
        """insert into collective.spec_revisions
        (spec_id,revision,base_unit,pack_multiple,perishable,attributes)
        values (%s,1,'piece',100,false,'{}')""",
        (ids["spec"],),
    )
    for letter in "ab":
        business = ids[letter]
        await db.execute(
            """insert into collective.quote_revisions
            (business_id,id,revision,supplier_id,spec_id,spec_revision,base_unit,
             pack_multiple,zone,offer_family,tier_kind,provenance,valid_from,valid_until)
            values (%s,%s,1,%s,%s,1,'piece',100,'sepang',%s,'all_unit','synthetic',%s,%s)""",
            (
                business,
                ids["quote_" + letter],
                ids["supplier"],
                ids["spec"],
                ids["family"],
                now - timedelta(hours=1),
                now + timedelta(hours=3),
            ),
        )
        await db.execute(
            """insert into collective.quote_tiers
            (business_id,quote_id,quote_revision,min_quantity,unit_price)
            values (%s,%s,1,1000,0.30)""",
            (business, ids["quote_" + letter]),
        )
        await db.execute(
            "update collective.quote_revisions set sealed=true where business_id=%s", (business,)
        )
        await db.execute(
            """insert into collective.reorder_needs
            (id,business_id,spec_id,spec_revision,quantity,base_unit,pack_multiple,zone,
             saving_threshold,min_credit_days,delivery_mode,order_start,order_end,
             arrival_start,arrival_end,state,provenance)
            values (%s,%s,%s,1,1000,'piece',100,'sepang',0.05,30,'per_business',%s,%s,%s,%s,
                    'selected','synthetic')""",
            (
                ids["need_" + letter],
                business,
                ids["spec"],
                now,
                now + timedelta(hours=2),
                now + timedelta(days=4),
                now + timedelta(days=5),
            ),
        )
        text = "Synthetic fixture " + letter
        await db.execute(
            """insert into collective.sources
            (id,business_id,kind,status,sha256,text_content,provenance)
            values (%s,%s,'text','available',%s,%s,'synthetic')""",
            (ids["source_" + letter], business, hashlib.sha256(text.encode()).hexdigest(), text),
        )
        await db.execute(
            """insert into collective.proposals
            (id,business_id,source_id,kind,current_revision) values (%s,%s,%s,'purchase',1)""",
            (ids["proposal_" + letter], business, ids["source_" + letter]),
        )
        await db.execute(
            """insert into collective.proposal_revisions
            (business_id,proposal_id,revision,payload,schema_version) values (%s,%s,1,'{}','1')""",
            (business, ids["proposal_" + letter]),
        )
    await db.execute(
        """insert into collective.pools
        (id,supplier_id,spec_id,spec_revision,offer_family,delivery_mode,deadline,state)
        values (%s,%s,%s,1,%s,'per_business',%s,'ready')""",
        (ids["pool"], ids["supplier"], ids["spec"], ids["family"], now + timedelta(hours=2)),
    )
    for letter in "ab":
        await db.execute(
            """insert into collective.pool_members (pool_id,business_id,need_id,state)
            values (%s,%s,%s,'approved')""",
            (ids["pool"], ids[letter], ids["need_" + letter]),
        )
        terms = {
            "need_id": str(ids["need_" + letter]),
            "need_revision": 1,
            "supplier_id": str(ids["supplier"]),
            "spec_id": str(ids["spec"]),
            "spec_revision": 1,
            "quantity": "1000",
            "base_unit": "piece",
            "delivery_mode": "per_business",
            "delivery_location": None,
            "credit_days": 30,
            "delivery_window": {
                "start": (now + timedelta(days=4)).isoformat(),
                "end": (now + timedelta(days=5)).isoformat(),
            },
            "conditions_fingerprint": "f" * 64,
        }
        await db.execute(
            """insert into collective.bound_approvals
            (id,business_id,need_id,owner_id,pool_id,terms,cap,expires_at,approved_at,
             evaluation_revision,quote_id,quote_revision)
            values (%s,%s,%s,%s,%s,%s,300,%s,%s,1,%s,1)""",
            (
                ids["approval_" + letter],
                ids[letter],
                ids["need_" + letter],
                ids["owner_" + letter],
                ids["pool"],
                Jsonb(terms),
                now + timedelta(hours=1),
                now,
                ids["quote_" + letter],
            ),
        )
    await db.execute(
        """insert into collective.pool_evaluations
        (pool_id,revision,evaluated_at,snapshot) values (%s,1,%s,'{}')""",
        (ids["pool"], now),
    )
    return ids


async def insert_purchase(db, r, *, letter="a", source_letter="a", purchase_id=None):
    await db.execute(
        """insert into collective.purchases
        (business_id,id,revision,supplier_id,source_id,proposal_id,proposal_revision,purpose,
         confirmed_by,provenance,total)
        values (%s,%s,1,%s,%s,%s,1,'historical',%s,'human',315)""",
        (
            r[letter],
            purchase_id or uuid4(),
            r["supplier"],
            r["source_" + source_letter],
            r["proposal_" + source_letter],
            r["owner_" + letter],
        ),
    )


async def insert_release(db, r):
    await db.execute(
        """insert into collective.releases
        (id,pool_id,evaluation_revision,snapshot) values (%s,%s,1,'{}')""",
        (r["release"], r["pool"]),
    )


async def insert_order(db, r, letter="a", *, amount="285.00"):
    await db.execute(
        """insert into collective.purchase_orders
        (id,business_id,release_id,need_id,approval_id,supplier_id,spec_id,spec_revision,
         quantity,amount,decision_amount,snapshot)
        values (%s,%s,%s,%s,%s,%s,%s,1,1000,%s,%s,'{}')""",
        (
            uuid4(),
            r[letter],
            r["release"],
            r["need_" + letter],
            r["approval_" + letter],
            r["supplier"],
            r["spec"],
            amount,
            amount,
        ),
    )


async def test_cross_business_source_cannot_be_confirmed(db, records):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        async with db.transaction():
            await insert_purchase(db, records, source_letter="b")


async def test_duplicate_confirmation_and_negative_money_fail(db, records):
    await insert_purchase(db, records)
    with pytest.raises(psycopg.errors.UniqueViolation):
        async with db.transaction():
            await insert_purchase(db, records)
    with pytest.raises(psycopg.errors.CheckViolation) as invalid_price:
        async with db.transaction():
            await db.execute(
                """insert into collective.purchase_lines
                (business_id,purchase_id,purchase_revision,line,source_name,unit_price)
                select business_id,id,1,0,'Test cups',-1 from collective.purchases
                where business_id=%s""",
                (records["a"],),
            )
    assert invalid_price.value.diag.constraint_name == "purchase_lines_unit_price_check"


async def test_sealed_quote_rejects_new_tiers_and_price_edits(db, records):
    with pytest.raises(psycopg.errors.CheckViolation, match="sealed"):
        async with db.transaction():
            await db.execute(
                """insert into collective.quote_tiers
                values (%s,%s,1,2000,0.27)""",
                (records["a"], records["quote_a"]),
            )
    with pytest.raises(psycopg.errors.CheckViolation, match="Immutable"):
        async with db.transaction():
            await db.execute(
                "update collective.quote_revisions set delivery_fee=0 where business_id=%s",
                (records["a"],),
            )


async def test_binding_cap_is_immutable_and_void_cannot_revive(db, records):
    with pytest.raises(psycopg.errors.CheckViolation, match="Immutable"):
        async with db.transaction():
            await db.execute(
                "update collective.bound_approvals set cap=999,revision=2 where id=%s",
                (records["approval_a"],),
            )
    await db.execute(
        "update collective.bound_approvals set state='void',revision=2 where id=%s",
        (records["approval_a"],),
    )
    with pytest.raises(psycopg.errors.CheckViolation, match="Immutable"):
        async with db.transaction():
            await db.execute(
                "update collective.bound_approvals set state='valid',revision=3 where id=%s",
                (records["approval_a"],),
            )
    row = await (
        await db.execute(
            "select history from collective.bound_approvals where id=%s", (records["approval_a"],)
        )
    ).fetchone()
    assert row["history"]["1"]["cap"] == 300


async def test_coordinator_cannot_mint_owner_approval_or_accept_private_rule(db, records):
    rule = uuid4()
    await db.execute(
        """insert into collective.learned_rules
        (id,business_id,scope,kind,context_key,evidence_digest,proposed_value,sample_count,distinct_businesses)
        values (%s,%s,'private','observed_fee','supplier-zone','digest','{}',3,1)""",
        (rule, records["a"]),
    )
    await set_context(db, "coordinator", None)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.transaction():
            await db.execute("insert into collective.bound_approvals (id) values (%s)", (uuid4(),))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.transaction():
            await db.execute(
                "update collective.learned_rules set state='accepted',revision=2 where id=%s",
                (rule,),
            )


async def test_po_requires_current_permission_and_respects_cap(db, records):
    await insert_release(db, records)
    with pytest.raises(psycopg.errors.CheckViolation, match="approval"):
        async with db.transaction():
            await insert_order(db, records, amount="301.00")
    await db.execute(
        "update collective.bound_approvals set state='void',revision=2 where id=%s",
        (records["approval_a"],),
    )
    with pytest.raises(psycopg.errors.CheckViolation, match="approval"):
        async with db.transaction():
            await insert_order(db, records)


async def test_partial_release_cannot_commit_and_complete_release_cannot_duplicate(db, records):
    with pytest.raises(psycopg.errors.CheckViolation, match="Incomplete release"):
        async with db.transaction():
            await insert_release(db, records)
            await insert_order(db, records)
            await db.execute("set constraints all immediate")
    await insert_release(db, records)
    await insert_order(db, records, "a")
    await insert_order(db, records, "b")
    await db.execute("set constraints all immediate")
    with pytest.raises(psycopg.errors.UniqueViolation):
        async with db.transaction():
            await insert_release(db, records)
    with pytest.raises(psycopg.errors.CheckViolation, match="Immutable"):
        async with db.transaction():
            await db.execute(
                "update collective.purchase_orders set amount=1 where release_id=%s",
                (records["release"],),
            )


async def test_need_has_one_active_reservation_and_history_requires_revision(db, records):
    with pytest.raises(psycopg.errors.CheckViolation, match="revision"):
        async with db.transaction():
            await db.execute(
                "update collective.reorder_needs set quantity=2000 where id=%s",
                (records["need_a"],),
            )
    await db.execute(
        "update collective.reorder_needs set quantity=2000,revision=2 where id=%s",
        (records["need_a"],),
    )
    row = await (
        await db.execute(
            "select history from collective.reorder_needs where id=%s", (records["need_a"],)
        )
    ).fetchone()
    assert row["history"]["1"]["quantity"] == 1000
    second_pool = uuid4()
    await db.execute(
        """insert into collective.pools
        (id,supplier_id,spec_id,spec_revision,offer_family,delivery_mode,deadline,state)
        select %s,supplier_id,spec_id,spec_revision,offer_family,delivery_mode,deadline,'offered'
        from collective.pools where id=%s""",
        (second_pool, records["pool"]),
    )
    with pytest.raises(psycopg.errors.UniqueViolation, match="need_active_reservation"):
        async with db.transaction():
            await db.execute(
                """insert into collective.pool_members values (%s,%s,%s,'offered')""",
                (second_pool, records["a"], records["need_a"]),
            )


async def test_staff_limit_and_synthetic_provenance_cannot_be_bypassed(db, records):
    await db.execute(
        "update collective.businesses set staff_limit=100 where id=%s", (records["a"],)
    )
    await set_context(db, "tenant", records["staff"])
    for total, provenance in [("315.00", "human"), ("50.00", "synthetic")]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with db.transaction():
                await db.execute(
                    """insert into collective.purchases
                    (business_id,id,revision,supplier_id,source_id,proposal_id,proposal_revision,
                     purpose,confirmed_by,provenance,total)
                    values (%s,%s,1,%s,%s,%s,1,'historical',%s,%s,%s)""",
                    (
                        records["a"],
                        uuid4(),
                        records["supplier"],
                        records["source_a"],
                        records["proposal_a"],
                        records["staff"],
                        provenance,
                        total,
                    ),
                )
    await db.execute(
        """insert into collective.purchases
        (business_id,id,revision,supplier_id,source_id,proposal_id,proposal_revision,
         purpose,confirmed_by,provenance,total)
        values (%s,%s,1,%s,%s,%s,1,'historical',%s,'human',50)""",
        (
            records["a"],
            uuid4(),
            records["supplier"],
            records["source_a"],
            records["proposal_a"],
            records["staff"],
        ),
    )


async def test_available_source_cannot_be_replaced_after_confirmation(db, records):
    with pytest.raises(psycopg.errors.CheckViolation, match="source is immutable"):
        async with db.transaction():
            await db.execute(
                "update collective.sources set text_content='different invoice' where id=%s",
                (records["source_a"],),
            )


async def test_network_rule_requires_five_and_admin_cannot_read_private_evidence(db, records):
    network, private_rule, network_rule = uuid4(), uuid4(), uuid4()
    await db.execute(
        "insert into collective.networks (id,name) values (%s,'Test network')", (network,)
    )
    await db.execute(
        "insert into collective.network_admins values (%s,%s)", (network, records["owner_b"])
    )
    await db.execute(
        """insert into collective.learned_rules
        (id,network_id,scope,kind,context_key,evidence_digest,proposed_value,sample_count,distinct_businesses)
        values (%s,%s,'network','observed_fee','context','network-digest','{}',3,3)""",
        (network_rule, network),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        async with db.transaction():
            await db.execute(
                """update collective.learned_rules set state='accepted',revision=2,
                accepted_by=%s,accepted_at=now() where id=%s""",
                (records["owner_b"], network_rule),
            )
    await db.execute(
        """insert into collective.learned_rules
        (id,business_id,scope,kind,context_key,evidence_digest,proposed_value,sample_count,distinct_businesses)
        values (%s,%s,'private','observed_fee','context','private-digest','{}',3,1)""",
        (private_rule, records["a"]),
    )
    await db.execute(
        "insert into collective.rule_evidence (rule_id,business_id,source_id) values (%s,%s,%s)",
        (private_rule, records["a"], records["source_a"]),
    )
    await set_context(db, "tenant", records["owner_b"])
    assert await (await db.execute("select id from collective.learned_rules")).fetchall() == []
    assert await (await db.execute("select * from collective.rule_evidence")).fetchall() == []


async def test_release_cannot_commit_with_permission_invalidated_after_po_insert(db, records):
    with pytest.raises(psycopg.errors.CheckViolation, match="Invalid release approvals"):
        async with db.transaction():
            await insert_release(db, records)
            await insert_order(db, records, "a")
            await insert_order(db, records, "b")
            await db.execute(
                "update collective.bound_approvals set state='void',revision=2 where id=%s",
                (records["approval_a"],),
            )
            await db.execute("set constraints all immediate")
