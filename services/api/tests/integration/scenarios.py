from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.fixtures import scenario
from tests.integration.conftest import local_test_url


async def clone_business_data(access_seed, letters, *, pool_opt_in, include_history=True):
    def original(value):
        return uuid5(NAMESPACE_URL, f"orbyte-run:{access_seed['run']}:{UUID(str(value))}")

    data = scenario()
    businesses = [original(b["id"]) for b in data["businesses"] if b["name"][-1].lower() in letters]
    tables = (
        "canonical_specs",
        "spec_revisions",
        "businesses",
        "business_memberships",
        "business_suppliers",
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
            column = {"canonical_specs": "id", "spec_revisions": "spec_id", "businesses": "id"}.get(
                table, "business_id"
            )
            ids = (
                [original(data["spec"]["id"])]
                if table in {"canonical_specs", "spec_revisions"}
                else businesses
            )
            records[table] = await (
                await conn.execute(
                    sql.SQL("select * from collective.{} where {}=any(%s)").format(
                        sql.Identifier(table), sql.Identifier(column)
                    ),
                    (ids,),
                )
            ).fetchall()
        remap = {row["id"]: uuid4() for rows in records.values() for row in rows if "id" in row}
        sealed = []
        for table, rows in records.items():
            if not include_history and table in {"purchases", "purchase_lines"}:
                continue
            for row in rows:
                values = {
                    key: remap.get(value, value) if isinstance(value, UUID) else value
                    for key, value in row.items()
                }
                if table == "businesses":
                    values["pool_opt_in"] = pool_opt_in
                if values.get("sealed"):
                    values["sealed"] = False
                    sealed.append((table, values["business_id"], values["id"], values["revision"]))
                await conn.execute(
                    sql.SQL("insert into collective.{} ({}) values ({})").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, values)),
                        sql.SQL(",").join(sql.Placeholder() for _ in values),
                    ),
                    [Jsonb(v) if isinstance(v, dict | list) else v for v in values.values()],
                )
        for table, business, identity, revision in sealed:
            await conn.execute(
                sql.SQL("""update collective.{} set sealed=true
                where business_id=%s and id=%s and revision=%s""").format(sql.Identifier(table)),
                (business, identity, revision),
            )
    return lambda value: remap.get(original(value), original(value))
