"""Add declared synthetic learning evidence to one disposable browser-test run."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from collective.repositories.db import require_local_database
from dotenv import dotenv_values
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]


def insert(conn, table, values):
    conn.execute(
        sql.SQL("insert into collective.{} ({}) values ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ),
        [Jsonb(v) if isinstance(v, dict | list) else v for v in values.values()],
    )


def prepare(run, mode):
    if not re.fullmatch(r"e2e-[a-z0-9-]{1,35}", run):
        raise ValueError("Learning test evidence requires an isolated e2e run")
    saved = json.loads((ROOT / f".cache/demo-{run}.json").read_text())
    fixture = json.loads(
        (ROOT / "fixtures/scenarios/five-business-history.json").read_text()
    )
    scenario = json.loads((ROOT / "fixtures/scenarios/three-cafes.json").read_text())
    assert fixture["provenance"] == scenario["provenance"] == "synthetic"

    def uid(value):
        return uuid5(NAMESPACE_URL, f"orbyte-run:{run}:{UUID(str(value))}")

    def identity(key):
        return uuid5(NAMESPACE_URL, f"orbyte-learning-e2e:{run}:{key}")

    business, spec = uid(scenario["businesses"][0]["id"]), uid(scenario["spec"]["id"])
    supplier = uid(scenario["suppliers"][1]["id"])
    private_ids = []
    env = dotenv_values(ROOT / ".env")
    with psycopg.connect(
        require_local_database(env["MIGRATION_DATABASE_URL"]), row_factory=dict_row
    ) as conn:
        own = conn.execute(
            """select b.* from collective.businesses b
            join collective.business_memberships m on m.business_id=b.id
            where b.id=%s and m.user_id=%s and m.role='owner'""",
            (business, UUID(saved["accounts"]["owner-a"]["id"])),
        ).fetchone()
        assert own and own["name"] == "Café A"

        def source(target, key, text):
            source_id = identity(key + ":source")
            insert(
                conn,
                "sources",
                {
                    "id": source_id,
                    "business_id": target,
                    "kind": "text",
                    "status": "available",
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "text_content": text,
                    "provenance": "synthetic",
                    "price_bearing": True,
                },
            )
            private_ids.append(str(source_id))
            return source_id

        def invoice(target, key, fee, at):
            purchase = identity(key + ":purchase")
            document = "SYNTHETIC-" + key
            total = Decimal("300.00") + Decimal(fee)
            evidence = source(
                target,
                key,
                f"SYNTHETIC TEST INVOICE {document}. Supplier B. Date {at}. "
                f"Buyer reference {target}. 1000 paper cups at MYR0.30: goods MYR300.00. "
                f"Delivery fee MYR{fee}. Tax MYR0.00; discount MYR0.00; surcharge MYR0.00. "
                f"Total MYR{total}. Sepang, separate business delivery, credit 30 days.",
            )
            insert(
                conn,
                "purchases",
                {
                    "id": purchase,
                    "business_id": target,
                    "revision": 1,
                    "supplier_id": supplier,
                    "source_id": evidence,
                    "document_number": document,
                    "document_date": at,
                    "currency": "MYR",
                    "purpose": "historical",
                    "goods_total": Decimal("300.00"),
                    "delivery_fee": Decimal(fee),
                    "tax_rate": Decimal(0),
                    "discount": Decimal(0),
                    "surcharge": Decimal(0),
                    "total": total,
                    "credit_days": 30,
                    "delivery_mode": "per_business",
                    "delivery_zone": "sepang",
                    "provenance": "synthetic",
                },
            )
            insert(
                conn,
                "purchase_lines",
                {
                    "business_id": target,
                    "purchase_id": purchase,
                    "purchase_revision": 1,
                    "line": 0,
                    "source_name": "250ml paper cup",
                    "quantity": Decimal(1000),
                    "base_unit": "piece",
                    "unit_price": Decimal("0.30"),
                    "line_total": Decimal("300.00"),
                    "spec_id": spec,
                    "spec_revision": 1,
                },
            )
            conn.execute(
                "update collective.purchases set sealed=true where business_id=%s and id=%s",
                (target, purchase),
            )
            private_ids.append(str(purchase))

        if mode == "network":
            for key in fixture["business_keys"]:
                target = identity(key + ":business")
                insert(
                    conn,
                    "businesses",
                    {
                        "id": target,
                        "network_id": own["network_id"],
                        "name": key,
                        "zone": "sepang",
                        "locale": "en",
                    },
                )
                private_ids.append(str(target))
                for template in fixture["invoices_per_business"]:
                    invoice(
                        target,
                        key + ":" + template["key"],
                        template["delivery_fee"],
                        datetime.fromisoformat(template["at"]).date(),
                    )
        elif mode == "private-more":
            invoice(business, "private:october", "35.00", datetime(2026, 10, 1).date())
        else:
            assert (
                conn.execute(
                    "select 1 from collective.bound_approvals where business_id=%s",
                    (business,),
                ).fetchone()
                is None
            )
            warning = json.loads(
                (ROOT / "fixtures/scenarios/fee-warning.json").read_text()
            )
            original_id = uuid5(NAMESPACE_URL, "orbyte-fixture:quote-a-b")
            row = conn.execute(
                """select * from collective.quote_revisions
                where business_id=%s and id=%s order by revision desc limit 1""",
                (business, uid(original_id)),
            ).fetchone()
            assert row and row["sealed"] and row["provenance"] == "synthetic"
            insert(
                conn,
                "quote_revisions",
                row
                | {
                    "revision": row["revision"] + 1,
                    "delivery_fee": Decimal(warning["quoted_delivery"]),
                    "sealed": False,
                },
            )
            for tier in conn.execute(
                """select * from collective.quote_tiers
                where business_id=%s and quote_id=%s and quote_revision=%s""",
                (business, row["id"], row["revision"]),
            ).fetchall():
                insert(
                    conn, "quote_tiers", tier | {"quote_revision": row["revision"] + 1}
                )
            conn.execute(
                """update collective.quote_revisions set sealed=true
                where business_id=%s and id=%s and revision=%s""",
                (business, row["id"], row["revision"] + 1),
            )
            for template in fixture["invoices_per_business"]:
                invoice(
                    business,
                    "private:" + template["key"],
                    template["delivery_fee"],
                    datetime.fromisoformat(template["at"]).date(),
                )
            now = datetime.now(UTC)
            for key, quantity, at in (
                ("opening", "1000", now - timedelta(days=21)),
                ("closing", "0", now - timedelta(days=1)),
            ):
                evidence = source(
                    business,
                    key,
                    f"SYNTHETIC stock count: {quantity} paper cups at {at.isoformat()}. No arrival is implied by an invoice.",
                )
                insert(
                    conn,
                    "stock_observations",
                    {
                        "id": identity(key + ":stock"),
                        "business_id": business,
                        "spec_id": spec,
                        "spec_revision": 1,
                        "quantity": Decimal(quantity),
                        "observed_at": at,
                        "source_id": evidence,
                        "provenance": "synthetic",
                    },
                )
    return {"business_id": str(business), "private_ids": private_ids}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument(
        "--mode", choices=("private", "private-more", "network"), required=True
    )
    args = parser.parse_args()
    print(json.dumps(prepare(args.run, args.mode)))
