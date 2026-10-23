import asyncio
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from tests.fixtures import fixture_id, scenario
from tests.integration.conftest import local_test_url
from tests.integration.scenarios import clone_business_data


@pytest.fixture
async def cohort(access_seed):
    return await clone_business_data(access_seed, "abc", pool_opt_in=True)


def business(cohort, letter):
    return cohort(scenario()["businesses"]["abc".index(letter)]["id"])


def need(cohort, letter):
    return cohort(scenario()["businesses"]["abc".index(letter)]["need_id"])


async def discover(client, cohort, auth_headers, letter="a", *, key=None, revision=1):
    return await client.post(
        f"/v1/businesses/{business(cohort, letter)}/pools/discover",
        headers=auth_headers("owner-" + letter) | {"Idempotency-Key": key or str(uuid4())},
        json={"need_id": str(need(cohort, letter)), "expected_revision": revision},
    )


async def test_three_cafes_reserve_once_and_receive_only_own_offer(api, cohort, auth_headers):
    client, database = api
    key = str(uuid4())
    response = await discover(client, cohort, auth_headers, key=key)
    assert response.status_code == 200
    result = response.json()
    assert result["state"] == "found"
    assert result["offer"]["decision_delivered_cost"] == "255.00"
    assert result["offer"]["member_count"] == 3
    assert result["offer"]["release_state"] == "waiting_for_approvals"
    assert result["need"]["revision"] == 2 and result["need"]["state"] == "reserved"
    assert (await discover(client, cohort, auth_headers, key=key)).json() == result
    pool = result["offer"]["pool_id"]
    for letter in "abc":
        own = await client.get(
            f"/v1/businesses/{business(cohort, letter)}/pools/{pool}/offer",
            headers=auth_headers("owner-" + letter),
        )
        assert own.status_code == 200
        assert own.json()["need_id"] == str(need(cohort, letter))
        assert own.json()["historical_saving"] == "60.00"
        for other in set("abc") - {letter}:
            assert str(business(cohort, other)) not in own.text
            assert str(need(cohort, other)) not in own.text
            assert str(cohort(fixture_id("quote-" + other + "-b"))) not in own.text
    async with database.transaction("coordinator", None) as conn:
        rows = await (
            await conn.execute(
                "select state,revision,quantity from collective.reorder_needs where spec_id=%s",
                (cohort(scenario()["spec"]["id"]),),
            )
        ).fetchall()
        assert len(rows) == 3 and all(
            r["state"] == "reserved" and r["quantity"] == 1000 for r in rows
        )
        snapshot = (
            await (
                await conn.execute(
                    "select snapshot from collective.pool_evaluations where pool_id=%s", (pool,)
                )
            ).fetchone()
        )["snapshot"]
        assert len(snapshot["members"]) == 3
        assert all(m["need"]["revision"] == 2 for m in snapshot["members"])
        assert (
            await (
                await conn.execute(
                    """select count(*) as n from collective.purchase_orders
                    where business_id=any(%s)""",
                    ([business(cohort, letter) for letter in "abc"],),
                )
            ).fetchone()
        )["n"] == 0


async def test_discovery_race_reuses_one_reservation_for_different_owners(
    api, cohort, auth_headers
):
    client, database = api
    first, second = await asyncio.gather(
        discover(client, cohort, auth_headers, "a"), discover(client, cohort, auth_headers, "b")
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["offer"]["pool_id"] == second.json()["offer"]["pool_id"]
    async with database.transaction("coordinator", None) as conn:
        pools = await (
            await conn.execute(
                "select id from collective.pools where spec_id=%s",
                (cohort(scenario()["spec"]["id"]),),
            )
        ).fetchall()
        assert len(pools) == 1
        members = await (
            await conn.execute(
                "select need_id from collective.pool_members where pool_id=%s", (pools[0]["id"],)
            )
        ).fetchall()
        assert len(members) == len({r["need_id"] for r in members}) == 3


async def test_pool_reads_and_commands_preserve_business_and_price_permissions(
    api, cohort, auth_headers
):
    client, _ = api
    path = f"/v1/businesses/{business(cohort, 'a')}/pools/discover"
    body = {"need_id": str(need(cohort, "a")), "expected_revision": 1}
    staff = await client.post(
        path, headers=auth_headers("staff") | {"Idempotency-Key": str(uuid4())}, json=body
    )
    assert staff.status_code == 403
    cross = await client.post(
        path,
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json=body | {"need_id": str(need(cohort, "b"))},
    )
    assert cross.status_code == 404
    own = (await discover(client, cohort, auth_headers)).json()
    pool = own["offer"]["pool_id"]
    route = f"/v1/businesses/{business(cohort, 'a')}/pools/{pool}/offer"
    assert (await client.get(route, headers=auth_headers("owner-b"))).status_code == 404
    assert (await client.get(route, headers=auth_headers("admin"))).status_code == 404
    hidden = await client.get(route, headers=auth_headers("staff"))
    assert hidden.status_code == 200 and hidden.json()["price_visibility"] == "hidden"
    assert "decision_delivered_cost" not in hidden.json()
    assert "historical_comparable_cost" not in hidden.json()
    assert "255.00" not in hidden.text


@pytest.mark.parametrize(
    "change",
    [
        "opt_out",
        "suggested",
        "mapping",
        "capacity",
        "solo_approved",
        "incomplete_quote",
        "cheap_history",
        "supplier_mapping",
    ],
)
async def test_ineligible_third_cafe_is_excluded_and_actual_two_member_price_is_used(
    api, cohort, auth_headers, change
):
    client, _ = api
    async with await AsyncConnection.connect(
        local_test_url("MIGRATION_DATABASE_URL"), row_factory=dict_row
    ) as conn:
        if change == "opt_out":
            await conn.execute(
                "update collective.businesses set pool_opt_in=false where id=%s",
                (business(cohort, "c"),),
            )
        elif change in {"suggested", "mapping", "capacity"}:
            field, value = {
                "suggested": ("state", "suggested"),
                "mapping": ("mapping_id", None),
                "capacity": ("max_stock", "500"),
            }[change]
            from psycopg import sql

            extra = sql.SQL(",mapping_revision=null") if change == "mapping" else sql.SQL("")
            await conn.execute(
                sql.SQL(
                    "update collective.reorder_needs set {}=%s,revision=revision+1{} where id=%s"
                ).format(sql.Identifier(field), extra),
                (value, need(cohort, "c")),
            )
        elif change == "solo_approved":
            from datetime import UTC, datetime, timedelta

            now = datetime.now(UTC)
            await conn.execute(
                """insert into collective.bound_approvals
            (id,business_id,need_id,owner_id,terms,cap,expires_at,approved_at,
             evaluation_revision,quote_id,quote_revision)
            select %s,%s,%s,selected_by,'{}',315,%s,%s,1,%s,1
            from collective.reorder_needs where id=%s""",
                (
                    uuid4(),
                    business(cohort, "c"),
                    need(cohort, "c"),
                    now + timedelta(hours=1),
                    now,
                    cohort(fixture_id("quote-c-b")),
                    need(cohort, "c"),
                ),
            )
        elif change == "incomplete_quote":
            await conn.execute(
                """insert into collective.quote_revisions select
                (jsonb_populate_record(null::collective.quote_revisions,to_jsonb(q)||
                  jsonb_build_object('revision',2,'credit_days',null,'sealed',true))).*
                from collective.quote_revisions q where business_id=%s and id=%s and revision=1""",
                (business(cohort, "c"), cohort(fixture_id("quote-c-b"))),
            )
        elif change == "supplier_mapping":
            await conn.execute(
                """update collective.reorder_needs set mapping_id=%s,revision=revision+1
                where id=%s""",
                (cohort(fixture_id("mapping-c-a")), need(cohort, "c")),
            )
            different = uuid4()
            await conn.execute(
                "insert into collective.canonical_specs(id,name) values (%s,'Different product')",
                (different,),
            )
            await conn.execute(
                "insert into collective.spec_revisions values (%s,1,'piece',100,false,'{}')",
                (different,),
            )
            await conn.execute(
                """insert into collective.sku_mappings select
                (jsonb_populate_record(null::collective.sku_mappings,to_jsonb(m)||
                  jsonb_build_object('revision',2,'spec_id',%s::uuid))).*
                from collective.sku_mappings m where business_id=%s and id=%s and revision=1""",
                (different, business(cohort, "c"), cohort(fixture_id("mapping-c-b"))),
            )
        else:
            await conn.execute(
                """insert into collective.purchases select
                (jsonb_populate_record(null::collective.purchases,to_jsonb(p)||
                  jsonb_build_object('revision',2,'goods_total',200,'total',215,'sealed',false))).*
                from collective.purchases p where business_id=%s and id=%s and revision=1""",
                (business(cohort, "c"), cohort(fixture_id("history-c"))),
            )
            await conn.execute(
                """insert into collective.purchase_lines select
                (jsonb_populate_record(null::collective.purchase_lines,to_jsonb(l)||
                  jsonb_build_object('purchase_revision',2,'unit_price',0.20,'line_total',200))).*
                from collective.purchase_lines l
                where business_id=%s and purchase_id=%s and purchase_revision=1""",
                (business(cohort, "c"), cohort(fixture_id("history-c"))),
            )
            await conn.execute(
                "update collective.purchases set sealed=true where business_id=%s and revision=2",
                (business(cohort, "c"),),
            )
    response = await discover(client, cohort, auth_headers)
    assert response.status_code == 200
    assert response.json()["offer"]["member_count"] == 2
    assert response.json()["offer"]["decision_delivered_cost"] == "285.00"


async def test_no_pool_does_not_reserve_needs_and_stale_revision_is_rejected(
    api, cohort, auth_headers
):
    client, database = api
    stale = await discover(client, cohort, auth_headers, revision=99)
    assert stale.status_code == 409
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await conn.execute(
            "update collective.businesses set pool_opt_in=false where id=any(%s)",
            ([business(cohort, letter) for letter in "bc"],),
        )
    response = await discover(client, cohort, auth_headers)
    assert response.status_code == 200
    assert response.json()["state"] == "unavailable" and response.json()["offer"] is None
    assert response.json()["failure_codes"] == ["NO_ELIGIBLE_POOL"]
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select state,reserved_pool_id from collective.reorder_needs where id=%s",
                (need(cohort, "a"),),
            )
        ).fetchone()
        assert row["state"] == "selected" and row["reserved_pool_id"] is None
        assert (
            await (
                await conn.execute(
                    "select count(*) as n from collective.pools where spec_id=%s",
                    (cohort(scenario()["spec"]["id"]),),
                )
            ).fetchone()
        )["n"] == 0


async def test_current_private_solo_quote_can_block_a_historical_saving(api, cohort, auth_headers):
    client, _ = api
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await conn.execute(
            "update collective.businesses set pool_opt_in=false where id=%s",
            (business(cohort, "c"),),
        )
        for letter, supplier, tiers in (
            ("a", "a", [(1000, "0.255")]),
            ("a", "b", [(1000, "0.30"), (2000, "0.265")]),
            ("b", "b", [(1000, "0.30"), (2000, "0.265")]),
        ):
            quote = cohort(fixture_id(f"quote-{letter}-{supplier}"))
            await conn.execute(
                """insert into collective.quote_revisions select
                (jsonb_populate_record(null::collective.quote_revisions,to_jsonb(q)||
                  jsonb_build_object('revision',2,'sealed',false))).*
                from collective.quote_revisions q where business_id=%s and id=%s and revision=1""",
                (business(cohort, letter), quote),
            )
            for quantity, price in tiers:
                await conn.execute(
                    "insert into collective.quote_tiers values (%s,%s,2,%s,%s)",
                    (business(cohort, letter), quote, quantity, price),
                )
            await conn.execute(
                """update collective.quote_revisions set sealed=true
                where business_id=%s and id=%s and revision=2""",
                (business(cohort, letter), quote),
            )
    response = await discover(client, cohort, auth_headers)
    assert response.status_code == 200 and response.json()["state"] == "unavailable"
    comparisons = await client.get(
        f"/v1/businesses/{business(cohort, 'a')}/needs/{need(cohort, 'a')}/comparisons",
        headers=auth_headers("owner-a"),
    )
    assert comparisons.json()["historical_comparable_cost"] == "315.00"
    assert comparisons.json()["best_solo_cost"] == "270.00"


async def test_best_eligible_supplier_family_is_chosen_from_confirmed_records(
    api, cohort, auth_headers
):
    client, _ = api
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        for letter in "abc":
            quote = cohort(fixture_id("quote-" + letter + "-a"))
            await conn.execute(
                """insert into collective.quote_revisions select
                (jsonb_populate_record(null::collective.quote_revisions,to_jsonb(q)||
                  jsonb_build_object('revision',2,'sealed',false,'pooled_buyers_permitted',true))).*
                from collective.quote_revisions q where business_id=%s and id=%s and revision=1""",
                (business(cohort, letter), quote),
            )
            for quantity, price in ((1000, "0.30"), (2000, "0.25"), (3000, "0.20")):
                await conn.execute(
                    "insert into collective.quote_tiers values (%s,%s,2,%s,%s)",
                    (business(cohort, letter), quote, quantity, price),
                )
            await conn.execute(
                """update collective.quote_revisions set sealed=true
                where business_id=%s and id=%s and revision=2""",
                (business(cohort, letter), quote),
            )
    response = await discover(client, cohort, auth_headers)
    assert response.status_code == 200
    assert response.json()["offer"]["supplier_id"] == str(cohort(scenario()["suppliers"][0]["id"]))
    assert response.json()["offer"]["decision_delivered_cost"] == "215.00"
    assert response.json()["offer"]["member_count"] == 3


async def test_oversized_demo_search_stops_without_truncation_or_reservation(
    api, cohort, auth_headers
):
    client, database = api
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        for _ in range(10):
            await conn.execute(
                """insert into collective.reorder_needs select
                (jsonb_populate_record(null::collective.reorder_needs,to_jsonb(n)||
                  jsonb_build_object('id',%s::uuid))).*
                from collective.reorder_needs n where id=%s""",
                (uuid4(), need(cohort, "a")),
            )
    result = await discover(client, cohort, auth_headers)
    assert result.status_code == 200 and result.json()["state"] == "unavailable"
    assert result.json()["failure_codes"] == ["DISCOVERY_REVIEW_REQUIRED"]
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select count(*) as n from collective.pools where spec_id=%s",
                (cohort(scenario()["spec"]["id"]),),
            )
        ).fetchone()
        assert row["n"] == 0


async def test_offer_exact_deadline_is_unavailable_without_rewriting_its_snapshot(
    api, cohort, auth_headers, access_seed
):
    from datetime import datetime
    from uuid import UUID

    from collective.repositories.pools import own_offer

    client, database = api
    created = (await discover(client, cohort, auth_headers)).json()["offer"]
    deadline = datetime.fromisoformat(created["deadline"].replace("Z", "+00:00"))
    async with database.transaction(
        "tenant", UUID(access_seed["accounts"]["owner-a"]["id"])
    ) as conn:
        expired = await own_offer(conn, business(cohort, "a"), UUID(created["pool_id"]), deadline)
        assert expired.eligibility == "unavailable" and "OFFER_EXPIRED" in expired.failure_codes
        stored = (
            await (
                await conn.execute(
                    """select snapshot from collective.member_evaluations
                    where pool_id=%s and business_id=%s""",
                    (created["pool_id"], business(cohort, "a")),
                )
            ).fetchone()
        )["snapshot"]
        assert stored["eligibility"] == "eligible" and stored["failure_codes"] == []
