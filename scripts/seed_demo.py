"""Seed the declared synthetic scenario through local Auth, Storage and PostgreSQL."""

import argparse
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import psycopg
from collective.repositories.db import require_local_database
from dotenv import dotenv_values
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]


def fixture_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, "orbyte-fixture:" + name)


def insert(connection, table, values):
    query = sql.SQL(
        "insert into collective.{} ({}) values ({}) on conflict do nothing"
    ).format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, values)),
        sql.SQL(",").join(sql.Placeholder() for _ in values),
    )
    return connection.execute(query, list(values.values())).rowcount == 1


def seed(run: str = "demo", anchor: str = "now") -> dict:
    if not re.fullmatch(r"[a-z0-9-]{1,40}", run):
        raise ValueError("Run names use 1–40 lowercase letters, numbers or hyphens")
    env = dotenv_values(ROOT / ".env")
    operator_url = require_local_database(env["MIGRATION_DATABASE_URL"])
    api_url = env["SUPABASE_URL"].rstrip("/")
    if urlparse(api_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Synthetic seed refuses a non-local Auth/Storage target")
    data = json.loads((ROOT / "fixtures/scenarios/three-cafes.json").read_text())

    def uid(value):
        value = UUID(str(value))
        return (
            value
            if run == "demo"
            else uuid5(NAMESPACE_URL, f"orbyte-run:{run}:{value}")
        )

    credentials_path = ROOT / f".cache/demo-{run}.json"
    credentials_path.parent.mkdir(exist_ok=True)
    saved = (
        json.loads(credentials_path.read_text()) if credentials_path.exists() else {}
    )
    accounts = saved.get("accounts", {})
    identities = {"owner-" + letter: fixture_id("owner-" + letter) for letter in "abc"}
    identities |= {"staff": fixture_id("staff"), "admin": fixture_id("network-admin")}
    for name, identity in identities.items():
        accounts.setdefault(
            name,
            {
                "id": str(uid(identity)),
                "email": f"{name}.{run}@collective.test",
                "password": secrets.token_urlsafe(24),
            },
        )
    credentials_path.write_text(
        json.dumps({"run": run, "accounts": accounts}, indent=2) + "\n"
    )
    credentials_path.chmod(0o600)
    secret = env["SUPABASE_SECRET_KEY"]
    with httpx.Client(
        base_url=api_url,
        timeout=15,
        headers={
            "apikey": secret,
            "Authorization": "Bearer " + secret,
        },
    ) as client:
        for account in accounts.values():
            existing = client.get("/auth/v1/admin/users/" + account["id"])
            if existing.status_code == 404:
                response = client.post(
                    "/auth/v1/admin/users",
                    json={
                        "id": account["id"],
                        "email": account["email"],
                        "password": account["password"],
                        "email_confirm": True,
                    },
                )
                response.raise_for_status()
            else:
                existing.raise_for_status()
                if existing.json()["email"] != account["email"]:
                    raise ValueError(
                        "Seed identity collision; existing account was not modified"
                    )
        bucket = client.get("/storage/v1/bucket/collective-sources")
        if bucket.status_code == 404 or bucket.status_code == 400:
            client.post(
                "/storage/v1/bucket",
                json={
                    "id": "collective-sources",
                    "name": "collective-sources",
                    "public": False,
                    "file_size_limit": 10485760,
                },
            ).raise_for_status()
        else:
            bucket.raise_for_status()
            if bucket.json()["public"]:
                raise ValueError("Demo source bucket must be private")
        image = (ROOT / "fixtures/sources/delivered-invoice.png").read_bytes()
        for business in data["businesses"]:
            letter = business["name"][-1].lower()
            business_id = uid(business["id"])
            source_id = uid(fixture_id("source-" + letter))
            object_key = f"{business_id}/{source_id}/original.png"
            existing_object = client.head(
                "/storage/v1/object/collective-sources/" + object_key
            )
            if existing_object.status_code != 200:
                client.post(
                    "/storage/v1/object/collective-sources/" + object_key,
                    content=image,
                    headers={"Content-Type": "image/png", "x-upsert": "false"},
                ).raise_for_status()
        with psycopg.connect(operator_url, row_factory=dict_row) as connection:
            existing = connection.execute(
                "select order_start,order_end from collective.reorder_needs where id=%s",
                (uid(data["businesses"][0]["need_id"]),),
            ).fetchone()
            now = datetime.now(UTC)
            when = (
                now.replace(microsecond=0)
                if anchor == "now"
                else datetime.fromisoformat(anchor)
            )
            if when.tzinfo is None:
                raise ValueError("Seed anchor must include a timezone")
            if existing:
                if anchor != "now" and when != existing["order_start"]:
                    raise ValueError(
                        "Existing run uses a different anchor; choose a new --run"
                    )
                when = existing["order_start"]
                if existing["order_end"] <= now:
                    raise ValueError(
                        "Existing run expired; choose a new --run or reset the disposable local DB"
                    )
            network = uid(fixture_id("network"))
            insert(
                connection,
                "networks",
                {"id": network, "name": "Synthetic café network"},
            )
            insert(
                connection,
                "network_admins",
                {"network_id": network, "user_id": uid(identities["admin"])},
            )
            spec = data["spec"]
            spec_id = uid(spec["id"])
            insert(connection, "canonical_specs", {"id": spec_id, "name": spec["name"]})
            insert(
                connection,
                "spec_revisions",
                {
                    "spec_id": spec_id,
                    "revision": 1,
                    "base_unit": "piece",
                    "pack_multiple": spec["pack_multiple"],
                    "perishable": spec["perishable"],
                    "attributes": Jsonb(
                        {
                            k: spec[k]
                            for k in ("capacity_ml", "material", "lid_diameter_mm")
                        }
                    ),
                },
            )
            for supplier in data["suppliers"]:
                insert(
                    connection,
                    "suppliers",
                    {"id": uid(supplier["id"]), "name": supplier["name"]},
                )
            for business in data["businesses"]:
                letter = business["name"][-1].lower()
                business_id = uid(business["id"])
                owner = uid(business["owner_id"])
                insert(
                    connection,
                    "businesses",
                    {
                        "id": business_id,
                        "network_id": network,
                        "name": business["name"],
                        "zone": business["zone"],
                        "locale": business["locale"],
                        "staff_limit": "100.00",
                        "staff_can_view_prices": False,
                        "pool_opt_in": True,
                    },
                )
                insert(
                    connection,
                    "business_memberships",
                    {"business_id": business_id, "user_id": owner, "role": "owner"},
                )
                if letter == "a":
                    insert(
                        connection,
                        "business_memberships",
                        {
                            "business_id": business_id,
                            "user_id": uid(identities["staff"]),
                            "role": "staff",
                        },
                    )
                for supplier in data["suppliers"]:
                    supplier_id = uid(supplier["id"])
                    insert(
                        connection,
                        "business_suppliers",
                        {
                            "business_id": business_id,
                            "supplier_id": supplier_id,
                            "contact": "simulated:" + supplier["name"],
                            "credit_days": 30,
                        },
                    )
                    insert(
                        connection,
                        "sku_mappings",
                        {
                            "business_id": business_id,
                            "id": uid(
                                fixture_id(
                                    f"mapping-{letter}-{supplier['name'][-1].lower()}"
                                )
                            ),
                            "revision": 1,
                            "supplier_id": supplier_id,
                            "supplier_sku": "CUP250",
                            "source_name": spec["name"],
                            "spec_id": spec_id,
                            "spec_revision": 1,
                            "pack_factors": Jsonb(["100"]),
                            "confirmed": True,
                            "confirmed_by": owner,
                            "confirmed_at": when,
                            "provenance": "synthetic",
                        },
                    )
                    insert(
                        connection,
                        "conversations",
                        {
                            "id": uid(
                                fixture_id(
                                    f"conversation-{letter}-{supplier['name'][-1].lower()}"
                                )
                            ),
                            "business_id": business_id,
                            "supplier_id": supplier_id,
                        },
                    )
                historical = uid(fixture_id("history-" + letter))
                if insert(
                    connection,
                    "purchases",
                    {
                        "business_id": business_id,
                        "id": historical,
                        "revision": 1,
                        "supplier_id": uid(data["suppliers"][0]["id"]),
                        "document_number": "DEMO-HISTORY-" + letter,
                        "document_date": (when - timedelta(days=7)).date(),
                        "currency": "MYR",
                        "purpose": "historical",
                        "goods_total": "300.00",
                        "delivery_fee": "15.00",
                        "tax_rate": "0",
                        "discount": "0.00",
                        "surcharge": "0.00",
                        "total": "315.00",
                        "credit_days": 30,
                        "delivery_mode": "per_business",
                        "confirmed_by": owner,
                        "provenance": "synthetic",
                    },
                ):
                    insert(
                        connection,
                        "purchase_lines",
                        {
                            "business_id": business_id,
                            "purchase_id": historical,
                            "purchase_revision": 1,
                            "line": 0,
                            "source_name": spec["name"],
                            "supplier_sku": "CUP250",
                            "original_quantity": "1000",
                            "original_unit": "piece",
                            "original_unit_price": "0.30",
                            "quantity": "1000",
                            "base_unit": "piece",
                            "unit_price": "0.30",
                            "line_total": "300.00",
                            "spec_id": spec_id,
                            "spec_revision": 1,
                            "mapping_id": uid(fixture_id(f"mapping-{letter}-a")),
                            "mapping_revision": 1,
                        },
                    )
                    connection.execute(
                        "update collective.purchases set sealed=true where business_id=%s and id=%s and revision=1",
                        (business_id, historical),
                    )
                stock = {
                    "quantity": business["opening_stock"],
                    "observed_at": when.isoformat(),
                    "movements": [],
                }
                usage = {
                    "stated_daily_rate": business["usage_per_day"],
                    "rule_accepted": False,
                }
                insert(
                    connection,
                    "stock_observations",
                    {
                        "id": uid(fixture_id("stock-" + letter)),
                        "business_id": business_id,
                        "spec_id": spec_id,
                        "spec_revision": 1,
                        "quantity": business["opening_stock"],
                        "observed_at": when,
                        "confirmed_by": owner,
                        "provenance": "synthetic",
                    },
                )
                insert(
                    connection,
                    "reorder_needs",
                    {
                        "id": uid(business["need_id"]),
                        "business_id": business_id,
                        "spec_id": spec_id,
                        "spec_revision": 1,
                        "mapping_id": uid(fixture_id(f"mapping-{letter}-b")),
                        "mapping_revision": 1,
                        "quantity": business["quantity"],
                        "base_unit": "piece",
                        "pack_multiple": spec["pack_multiple"],
                        "zone": business["zone"],
                        "saving_threshold": business["threshold"],
                        "min_credit_days": 30,
                        "delivery_mode": "per_business",
                        "order_start": when,
                        "order_end": when + timedelta(hours=24),
                        "arrival_start": when + timedelta(hours=96),
                        "arrival_end": when + timedelta(hours=120),
                        "state": "selected",
                        "selected_by": owner,
                        "stock_snapshot": Jsonb(stock),
                        "usage_input": Jsonb(usage),
                        "lead_days": business["lead_days"],
                        "buffer_days": business["buffer_days"],
                        "max_stock": business["storage_capacity"],
                        "provenance": "synthetic",
                    },
                )
                source_id = uid(fixture_id("source-" + letter))
                object_key = f"{business_id}/{source_id}/original.png"
                insert(
                    connection,
                    "sources",
                    {
                        "id": source_id,
                        "business_id": business_id,
                        "kind": "image",
                        "status": "available",
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "original_object_key": object_key,
                        "mime_type": "image/png",
                        "size_bytes": len(image),
                        "page_count": 1,
                        "uploaded_by": owner,
                        "provenance": "synthetic",
                        "price_bearing": True,
                    },
                )
            for source in data["quotes"]:
                row = {
                    key: source[key]
                    for key in (
                        "revision",
                        "currency",
                        "base_unit",
                        "pooled_buyers_permitted",
                        "tier_kind",
                        "per_business_moq",
                        "aggregate_moq",
                        "delivery_mode",
                        "delivery_fee",
                        "free_delivery_threshold",
                        "waive_split_drop_fees",
                        "credit_days",
                        "tax_rate",
                        "discount",
                        "surcharge",
                        "conditions",
                    )
                }
                row |= {
                    key: uid(source[key])
                    for key in (
                        "id",
                        "business_id",
                        "supplier_id",
                        "spec_id",
                        "offer_family",
                    )
                }
                row |= {
                    "spec_revision": 1,
                    "pack_multiple": spec["pack_multiple"],
                    "zone": "sepang",
                    "conditions_complete": True,
                    "valid_from": when - timedelta(hours=1),
                    "valid_until": when + timedelta(hours=48),
                    "arrival_start": when + timedelta(hours=96),
                    "arrival_end": when + timedelta(hours=120),
                    "provenance": "synthetic",
                }
                if insert(connection, "quote_revisions", row):
                    for tier in source["tiers"]:
                        insert(
                            connection,
                            "quote_tiers",
                            {
                                "business_id": row["business_id"],
                                "quote_id": row["id"],
                                "quote_revision": 1,
                                **tier,
                            },
                        )
                    connection.execute(
                        "update collective.quote_revisions set sealed=true where business_id=%s and id=%s and revision=1",
                        (row["business_id"], row["id"]),
                    )
    result = {"run": run, "anchor": when.isoformat(), "accounts": accounts}
    credentials_path.write_text(json.dumps(result, indent=2) + "\n")
    credentials_path.chmod(0o600)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["three-cafes"], required=True)
    parser.add_argument("--anchor", default="now")
    parser.add_argument("--run", default="demo")
    args = parser.parse_args()
    result = seed(args.run, args.anchor)
    print(f"Seeded synthetic {args.scenario} run {args.run}; anchor {result['anchor']}")
    print(
        f"Local credentials: .cache/demo-{args.run}.json (ignored; contains passwords)"
    )


if __name__ == "__main__":
    main()
