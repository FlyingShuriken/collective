from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.fixtures import fixture_id, scenario
from tests.integration.conftest import local_test_url


@pytest.fixture
async def access_id(access_seed):
    def original(value):
        return uuid5(NAMESPACE_URL, f"orbyte-run:{access_seed['run']}:{UUID(str(value))}")

    source_business = original(scenario()["businesses"][0]["id"])
    tables = (
        "businesses",
        "business_memberships",
        "sku_mappings",
        "purchases",
        "purchase_lines",
        "stock_observations",
        "quote_revisions",
        "quote_tiers",
        "reorder_needs",
    )
    async with await AsyncConnection.connect(
        local_test_url("MIGRATION_DATABASE_URL"), row_factory=dict_row
    ) as conn:
        records = {}
        for table in tables:
            column = "id" if table == "businesses" else "business_id"
            records[table] = await (
                await conn.execute(
                    sql.SQL("select * from collective.{} where {}=%s").format(
                        sql.Identifier(table), sql.Identifier(column)
                    ),
                    (source_business,),
                )
            ).fetchall()
        remap = {row["id"]: uuid4() for rows in records.values() for row in rows if "id" in row}
        sealed = []
        for table, rows in records.items():
            for row in rows:
                values = {
                    key: remap.get(value, value) if isinstance(value, UUID) else value
                    for key, value in row.items()
                }
                if table == "businesses":
                    values["pool_opt_in"] = False
                if values.get("sealed"):
                    values["sealed"] = False
                    sealed.append((table, values["id"], values["revision"]))
                await conn.execute(
                    sql.SQL("insert into collective.{} ({}) values ({})").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, values)),
                        sql.SQL(",").join(sql.Placeholder() for _ in values),
                    ),
                    [
                        Jsonb(value) if isinstance(value, dict | list) else value
                        for value in values.values()
                    ],
                )
        for table, identity, revision in sealed:
            await conn.execute(
                sql.SQL(
                    """update collective.{} set sealed=true
                    where business_id=%s and id=%s and revision=%s"""
                ).format(sql.Identifier(table)),
                (remap[source_business], identity, revision),
            )
    return lambda value: remap.get(original(value), original(value))


def url(access_id, suffix):
    business = access_id(scenario()["businesses"][0]["id"])
    return f"/v1/businesses/{business}/{suffix}"


def need_id(access_id):
    return access_id(scenario()["businesses"][0]["need_id"])


async def test_confirmed_comparisons_are_real_and_scoped(api, access_id, auth_headers):
    client, _ = api
    path = url(access_id, f"needs/{need_id(access_id)}/comparisons")
    response = await client.get(path, headers=auth_headers("owner-a"))
    assert response.status_code == 200
    result = response.json()
    assert result["historical_comparable_cost"] == "315.00"
    assert result["best_solo_cost"] == "315.00"
    assert len(result["offers"]) == 2
    assert {o["cost"]["decision_cost"] for o in result["offers"]} == {"315.00"}
    assert result["need"]["mapping_confirmed"] is True
    assert (await client.get(path, headers=auth_headers("owner-b"))).status_code == 404
    hidden = (await client.get(path, headers=auth_headers("staff"))).json()
    assert hidden["price_visibility"] == "hidden"
    assert "offers" not in hidden and "best_solo_cost" not in hidden


async def test_latest_sealed_quote_revision_supersedes_price_and_missing_terms(
    api,
    access_id,
    auth_headers,
):
    client, _ = api
    business = access_id(scenario()["businesses"][0]["id"])
    quote = access_id(fixture_id("quote-a-b"))
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        cursor = await conn.execute(
            """select * from collective.quote_revisions
            where business_id=%s and id=%s and revision=1""",
            (business, quote),
        )
        columns = [column.name for column in cursor.description]
        values = dict(zip(columns, await cursor.fetchone(), strict=True))
        for revision, credit in ((2, 30), (3, None)):
            values.update(
                revision=revision, sealed=False, credit_days=credit, created_at=datetime.now(UTC)
            )
            await conn.execute(
                sql.SQL("insert into collective.quote_revisions ({}) values ({})").format(
                    sql.SQL(",").join(map(sql.Identifier, values)),
                    sql.SQL(",").join(sql.Placeholder() for _ in values),
                ),
                list(values.values()),
            )
            await conn.execute(
                "insert into collective.quote_tiers values (%s,%s,%s,1000,0.20)",
                (business, quote, revision),
            )
            await conn.execute(
                """update collective.quote_revisions set sealed=true
                where business_id=%s and id=%s and revision=%s""",
                (business, quote, revision),
            )
            await conn.commit()
            response = await client.get(
                url(access_id, f"needs/{need_id(access_id)}/comparisons"),
                headers=auth_headers("owner-a"),
            )
            assert response.status_code == 200
            row = next(o for o in response.json()["offers"] if o["quote_id"] == str(quote))
            assert row["quote_revision"] == revision
            if credit is not None:
                assert response.json()["best_solo_cost"] == "215.00"
            else:
                assert row["cost"]["decision_cost"] is None
                assert "MISSING_QUOTE_TERMS" in row["failure_codes"]
                assert "credit_days" in row["missing_fields"]
                assert response.json()["best_solo_cost"] == "315.00"
        changed_spec = uuid4()
        await conn.execute(
            "insert into collective.canonical_specs (id,name) values (%s,'Different cups')",
            (changed_spec,),
        )
        await conn.execute(
            "insert into collective.spec_revisions values (%s,1,'piece',100,false,'{}')",
            (changed_spec,),
        )
        values.update(revision=4, spec_id=changed_spec, credit_days=30, sealed=False)
        await conn.execute(
            sql.SQL("insert into collective.quote_revisions ({}) values ({})").format(
                sql.SQL(",").join(map(sql.Identifier, values)),
                sql.SQL(",").join(sql.Placeholder() for _ in values),
            ),
            list(values.values()),
        )
        await conn.execute(
            "insert into collective.quote_tiers values (%s,%s,4,1000,0.10)", (business, quote)
        )
        await conn.execute(
            """update collective.quote_revisions set sealed=true
            where business_id=%s and id=%s and revision=4""",
            (business, quote),
        )
        await conn.commit()
        updated = (
            await client.get(
                url(access_id, f"needs/{need_id(access_id)}/comparisons"),
                headers=auth_headers("owner-a"),
            )
        ).json()
        assert str(quote) not in {offer["quote_id"] for offer in updated["offers"]}


async def test_new_stock_changes_replenishment_without_counting_a_bill(
    api, access_id, auth_headers
):
    client, _ = api
    path = url(access_id, f"needs/{need_id(access_id)}/options")
    before = (await client.get(path, headers=auth_headers("owner-a"))).json()
    business = access_id(scenario()["businesses"][0]["id"])
    spec = access_id(scenario()["spec"]["id"])
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await conn.execute(
            """insert into collective.stock_observations
            (id,business_id,spec_id,spec_revision,quantity,observed_at,provenance)
            values (%s,%s,%s,1,100,now(),'synthetic')""",
            (uuid4(), business, spec),
        )
    after = (await client.get(path, headers=auth_headers("owner-a"))).json()
    assert Decimal(after["replenishment"]["stock_at_now"]) < 101
    assert after["replenishment"]["run_out_at"] < before["replenishment"]["run_out_at"]
    assert len(after["options"]) == 1  # Already selected quantities stay fixed.


def evaluation_body(access_id):
    from datetime import timedelta

    now = datetime.now(UTC)
    return {
        "mapping_id": str(access_id(fixture_id("mapping-a-b"))),
        "mapping_revision": 1,
        "quantity": "1000",
        "usage_per_day": "125",
        "lead_days": "4",
        "buffer_days": "2",
        "max_stock": "4000",
        "saving_threshold": "0.05",
        "min_credit_days": 30,
        "delivery_mode": "per_business",
        "order_window": {"start": now.isoformat(), "end": (now + timedelta(hours=24)).isoformat()},
        "arrival_window": {
            "start": (now + timedelta(days=4)).isoformat(),
            "end": (now + timedelta(days=5)).isoformat(),
        },
    }


async def test_owner_evaluates_and_selects_only_offered_quantity_once(api, access_id, auth_headers):
    client, _ = api
    body = evaluation_body(access_id)
    path = url(access_id, "needs/evaluate")
    headers = auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())}
    assert (
        await client.post(
            path, headers=auth_headers("staff") | {"Idempotency-Key": str(uuid4())}, json=body
        )
    ).status_code == 403
    response = await client.post(path, headers=headers, json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["need"]["state"] == "suggested"
    assert [Decimal(o["quantity"]) for o in result["options"]] == [1000, 2000, 3000]
    assert (await client.post(path, headers=headers, json=body)).json() == result
    different = await client.post(path, headers=headers, json=body | {"quantity": "2000"})
    assert different.status_code == 409
    select = url(access_id, f"needs/{result['need']['id']}/select")
    invalid = await client.post(
        select,
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": 1, "quantity": "5000"},
    )
    assert invalid.status_code == 422
    selected_headers = auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())}
    selected = await client.post(
        select, headers=selected_headers, json={"expected_revision": 1, "quantity": "2000"}
    )
    assert selected.status_code == 200
    assert selected.json()["state"] == "selected" and selected.json()["revision"] == 2
    assert (
        await client.post(
            select, headers=selected_headers, json={"expected_revision": 1, "quantity": "2000"}
        )
    ).json() == selected.json()
    stale = await client.post(
        select,
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": 1, "quantity": "1000"},
    )
    assert stale.status_code == 409
    page = (
        await client.get(url(access_id, "needs?limit=1"), headers=auth_headers("owner-a"))
    ).json()
    assert page["next_cursor"] is not None
    following = (
        await client.get(
            url(access_id, "needs?limit=1&cursor=" + page["next_cursor"]),
            headers=auth_headers("owner-a"),
        )
    ).json()
    assert page["items"][0]["id"] != following["items"][0]["id"]
    assert following["next_cursor"] is None


async def test_reserved_need_cannot_be_selected_again(api, access_id, auth_headers):
    client, _ = api
    target = need_id(access_id)
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await conn.execute(
            "update collective.reorder_needs set state='reserved',revision=revision+1 where id=%s",
            (target,),
        )
    response = await client.post(
        url(access_id, f"needs/{target}/select"),
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": 2, "quantity": "1000"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "NEED_COMMITTED"


async def test_valid_stated_need_can_be_selected_before_compatible_quotes_arrive(
    api, access_id, auth_headers
):
    from datetime import timedelta

    client, _ = api
    body = evaluation_body(access_id)
    now = datetime.now(UTC)
    body |= {
        "lead_days": "10",
        "arrival_window": {
            "start": (now + timedelta(days=10)).isoformat(),
            "end": (now + timedelta(days=11)).isoformat(),
        },
    }
    evaluated = await client.post(
        url(access_id, "needs/evaluate"),
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json=body,
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["options"] == []
    assert evaluated.json()["stated_quantity_valid"] is True
    selected = await client.post(
        url(access_id, f"needs/{evaluated.json()['need']['id']}/select"),
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": 1, "quantity": "1000"},
    )
    assert selected.status_code == 200
    assert selected.json()["state"] == "selected"


async def test_ledger_returns_confirmed_rows_with_price_projection(api, access_id, auth_headers):
    client, _ = api
    business = access_id(scenario()["businesses"][0]["id"])
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await conn.execute(
            """insert into collective.purchases select
          (jsonb_populate_record(null::collective.purchases,to_jsonb(p) ||
           jsonb_build_object('id',%s::uuid,'created_at',clock_timestamp(),
                              'document_number','Pagination check'))).*
          from collective.purchases p where business_id=%s""",
            (uuid4(), business),
        )
    response = await client.get(
        url(access_id, "ledger?kind=purchase&limit=1"), headers=auth_headers("owner-a")
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["total"] == "315.00"
    cursor = response.json()["next_cursor"]
    assert cursor is not None
    following = await client.get(
        url(access_id, "ledger?kind=purchase&limit=1&cursor=" + cursor),
        headers=auth_headers("owner-a"),
    )
    assert following.status_code == 200
    assert following.json()["items"][0]["id"] != response.json()["items"][0]["id"]
    assert following.json()["next_cursor"] is None
    hidden = (
        await client.get(url(access_id, "ledger?kind=purchase"), headers=auth_headers("staff"))
    ).json()
    assert "total" not in hidden["items"][0]
    assert (
        await client.get(url(access_id, "ledger"), headers=auth_headers("owner-b"))
    ).status_code == 404


@pytest.mark.parametrize(
    "patch,code",
    [({"usage_per_day": None}, "MISSING_USAGE"), ({"max_stock": None}, "MISSING_STORAGE_CAPACITY")],
)
async def test_missing_need_evidence_cannot_be_selected(api, access_id, auth_headers, patch, code):
    client, _ = api
    result = await client.post(
        url(access_id, "needs/evaluate"),
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json=evaluation_body(access_id) | patch,
    )
    assert result.status_code == 200
    assert result.json()["stated_quantity_valid"] is False
    assert result.json()["options"] == []
    assert code in result.json()["failure_codes"]
    select = await client.post(
        url(access_id, f"needs/{result.json()['need']['id']}/select"),
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": 1, "quantity": "1000"},
    )
    assert select.status_code == 422
