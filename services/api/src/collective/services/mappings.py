from decimal import Decimal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from collective.adapters.embeddings import EmbeddingGateway
from collective.adapters.models import ReaderGateway
from collective.adapters.replay import digest
from collective.adapters.storage import Storage
from collective.agents import matching
from collective.api.dto import JobStatus, MappingView, RequestMapping
from collective.api.errors import DomainError
from collective.domain.types import Pieces
from collective.repositories import jobs, pools, sources
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services import needs, solo, source_preparation
from collective.services.liaison_jobs import enqueue, supplier
from collective.services.permissions import membership
from collective.services.pool_commands import recalculate


def view(row):
    return MappingView.model_validate({key: row[key] for key in MappingView.model_fields})


async def lock_sku(connection, business, supplier_id, sku):
    await connection.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"mapping:{business}:{supplier_id}:{sku}",),
    )


async def next_revision(connection, business, supplier_id, sku):
    return (
        await (
            await connection.execute(
                """select coalesce(max(revision),0)+1 as revision from collective.sku_mappings
        where business_id=%s and supplier_id=%s and supplier_sku=%s""",
                (business, supplier_id, sku),
            )
        ).fetchone()
    )["revision"]


async def request(connection, subject, business, body, key, as_of):
    await membership(connection, subject, business, owner=True)
    payload = body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, "mappings", key, payload)
    if prior:
        return JobStatus.model_validate(prior)
    row = await sources.load(connection, business, body.source_id, lock=True)
    if row["sha256"] != body.source_sha256 or row["preparation_status"] != "ready":
        raise DomainError("SOURCE_NOT_PREPARED", "Prepare the current source first.", 409)
    await supplier(connection, business, body.supplier_id)
    payload |= {
        "requester": str(subject),
        "preparation_job_id": str(row["ingestion_job_id"]),
        "schema_version": matching.MATCH.schema_version,
        "prompt_version": matching.MATCH.prompt_version,
    }
    await set_context(connection, "coordinator", subject)
    job = await enqueue(
        connection,
        business,
        "matching",
        1,
        f"matching:{business}:{digest(payload)}",
        payload,
        key,
        as_of,
    )
    await lock_sku(connection, business, body.supplier_id, body.supplier_sku)
    existing = await (
        await connection.execute(
            "select id from collective.sku_mappings where business_id=%s and id=%s limit 1",
            (business, job["id"]),
        )
    ).fetchone()
    if existing is None:
        revision = await next_revision(connection, business, body.supplier_id, body.supplier_sku)
        await connection.execute(
            """insert into collective.sku_mappings
            (business_id,id,revision,supplier_id,supplier_sku,source_name,source_id,
             match_snapshot,provenance) values (%s,%s,%s,%s,%s,%s,%s,%s,'agent')""",
            (
                business,
                job["id"],
                revision,
                body.supplier_id,
                body.supplier_sku,
                body.source_name,
                body.source_id,
                Jsonb(
                    {
                        "flags": ["matching:pending"],
                        "candidates": [
                            item.model_dump(mode="json") for item in await catalogue(connection)
                        ],
                    }
                ),
            ),
        )
    result = jobs.public_status(job, as_of)
    await save_mutation(
        connection,
        subject,
        business,
        "mappings",
        key,
        body.model_dump(mode="json"),
        result.model_dump(mode="json"),
    )
    return result


async def authority(connection, job):
    subject = UUID(job["payload"]["requester"])
    await membership(connection, subject, job["business_id"], owner=True)
    row = await sources.load(connection, job["business_id"], UUID(job["payload"]["source_id"]))
    if row["preparation_status"] != "ready" or row["sha256"] != job["payload"]["source_sha256"]:
        raise DomainError("SOURCE_NOT_PREPARED", "Prepare the current source first.", 409)
    await supplier(connection, job["business_id"], UUID(job["payload"]["supplier_id"]))


async def catalogue(connection):
    rows = await (
        await connection.execute(
            """select r.* from collective.spec_revisions r where r.base_unit='piece'
        and not exists(select 1 from collective.spec_revisions n
          where n.spec_id=r.spec_id and n.revision>r.revision) order by r.spec_id"""
        )
    ).fetchall()
    result = []
    for row in rows:
        attrs = row["attributes"]
        if not {"capacity_ml", "lid_diameter_mm", "material"} <= attrs.keys():
            continue
        if attrs["material"] not in {"paper", "plastic"}:
            continue
        result.append(
            matching.CatalogueItem(
                key=f"{row['spec_id']}:{row['revision']}",
                text=f"{attrs['capacity_ml']}ml {attrs['material']} cup, "
                f"{attrs['material']} material, {attrs['lid_diameter_mm']}mm lid diameter",
                **{field: attrs[field] for field in ("capacity_ml", "lid_diameter_mm", "material")},
            )
        )
    return tuple(result)


async def handle(database, job, clock, *, settings, gateway=None, embeddings=None):
    payload = job["payload"]
    if (payload.get("schema_version"), payload.get("prompt_version")) != (
        matching.MATCH.schema_version,
        matching.MATCH.prompt_version,
    ):
        raise DomainError("STALE_AGENT_VERSION", "Request matching with the current version.", 409)
    body = RequestMapping.model_validate({key: payload[key] for key in RequestMapping.model_fields})
    async with database.transaction("coordinator", None) as connection:
        await authority(connection, job)
        items = await catalogue(connection)
    document = await source_preparation.for_job(database, Storage(settings), job)
    result = await matching.propose(
        gateway or ReaderGateway(settings),
        embeddings or EmbeddingGateway(settings),
        document,
        description=body.source_name,
        catalogue=items,
    )
    async with database.transaction("coordinator", None) as connection:
        await authority(connection, job)
        await jobs.hold_claim(connection, job, clock.now())
        await jobs.record_attempts(
            connection,
            job,
            result.attempts,
            result.reading.model_dump(mode="json") if result.reading else None,
        )
        await lock_sku(connection, job["business_id"], body.supplier_id, body.supplier_sku)
        existing = await (
            await connection.execute(
                """select * from collective.sku_mappings where business_id=%s and id=%s
                order by revision desc limit 1""",
                (job["business_id"], job["id"]),
            )
        ).fetchone()
        if existing is None or not existing["confirmed"]:
            revision = await next_revision(
                connection, job["business_id"], body.supplier_id, body.supplier_sku
            )
            selected = result.proposed_key.split(":") if result.proposed_key else None
            snapshot = {
                "reading": result.reading.model_dump(mode="json", exclude={"evidence"})
                if result.reading
                else None,
                "flags": list(result.flags),
                "evidence": [anchor | {"extracted_value": None} for anchor in result.evidence],
                "candidates": [item.model_dump(mode="json") for item in result.candidates],
                "scores": list(result.scores),
                "embedding_revision": result.embedding_revision,
                "embedding_mode": result.embedding_mode,
            }
            await connection.execute(
                """insert into collective.sku_mappings
                (business_id,id,revision,supplier_id,supplier_sku,source_name,spec_id,spec_revision,
                 pack_factors,source_id,source_unit,match_snapshot,provenance)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'agent')""",
                (
                    job["business_id"],
                    job["id"],
                    revision,
                    body.supplier_id,
                    body.supplier_sku,
                    body.source_name,
                    UUID(selected[0]) if selected else None,
                    int(selected[1]) if selected else None,
                    Jsonb(
                        [str(factor) for factor in result.reading.pack_factors]
                        if result.reading
                        else []
                    ),
                    body.source_id,
                    result.reading.source_unit
                    if result.reading
                    and result.reading.source_unit in {"piece", "carton", "box", "pack"}
                    else None,
                    Jsonb(snapshot),
                ),
            )
        await jobs.complete(connection, job, {"mapping_id": str(job["id"])}, clock.now())


async def confirm(connection, subject, business, identity, body, key, as_of):
    await membership(connection, subject, business, owner=True)
    route, payload = f"mappings/{identity}/confirm", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        return MappingView.model_validate(prior)
    row = await (
        await connection.execute(
            """select * from collective.sku_mappings where business_id=%s and id=%s
        order by revision desc limit 1""",
            (business, identity),
        )
    ).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    await set_context(connection, "coordinator", subject)
    specs = {body.spec_id, *([row["spec_id"]] if row["spec_id"] else [])}
    approvals = await pools.lock_record_inputs(connection, specs)
    await lock_sku(connection, business, row["supplier_id"], row["supplier_sku"])
    current = await (
        await connection.execute(
            """select max(revision) as revision from collective.sku_mappings
            where business_id=%s and id=%s""",
            (business, identity),
        )
    ).fetchone()
    if (
        row["confirmed"]
        or body.expected_revision != row["revision"]
        or current["revision"] != row["revision"]
    ):
        raise DomainError("STALE_REVISION", "Refresh the current mapping before confirming.", 409)
    if not body.acknowledge_flags:
        raise DomainError("REVIEW_REQUIRED", "Confirm the source attributes and pack size.")
    if f"{body.spec_id}:{body.spec_revision}" not in {
        item.key for item in await catalogue(connection)
    }:
        raise DomainError("UNSUPPORTED_SPEC", "Select a current catalogue specification.")
    product = Decimal(1)
    try:
        for factor in body.pack_factors:
            TypeAdapter(Pieces).validate_python(factor)
            product *= factor
        TypeAdapter(Pieces).validate_python(product)
    except ValueError as exc:
        raise DomainError(
            "INVALID_PACK", "Use whole piece counts within supported precision."
        ) from exc
    if body.source_unit == "piece" and body.pack_factors != (Decimal(1),):
        raise DomainError("INVALID_PACK", "A piece must map to one piece.")
    revision = await next_revision(connection, business, row["supplier_id"], row["supplier_sku"])
    await set_context(connection, "tenant", subject)
    saved = await (
        await connection.execute(
            """insert into collective.sku_mappings
        (business_id,id,revision,supplier_id,supplier_sku,source_name,spec_id,spec_revision,
         pack_factors,source_id,source_unit,match_snapshot,confirmed,confirmed_by,confirmed_at,provenance)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,'human') returning *""",
            (
                business,
                identity,
                revision,
                row["supplier_id"],
                row["supplier_sku"],
                row["source_name"],
                body.spec_id,
                body.spec_revision,
                Jsonb([str(factor) for factor in body.pack_factors]),
                row["source_id"],
                body.source_unit,
                Jsonb(row["match_snapshot"]),
                subject,
                as_of,
            ),
        )
    ).fetchone()
    await set_context(connection, "coordinator", subject)
    for pool in await (
        await connection.execute(
            """select * from collective.pools where spec_id=any(%s)
            and state<>'released' order by id""",
            (list(specs),),
        )
    ).fetchall():
        await recalculate(connection, pool, as_of)
    for approval in approvals:
        if approval["pool_id"] is None and approval["state"] == "valid":
            need = await needs.load_need(connection, approval["business_id"], approval["need_id"])
            evaluation = await solo.evaluate(connection, need, approval["quote_id"], as_of)
            await solo.revalidate(connection, approval, evaluation, as_of)
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind="owner",
            event_type="mapping_confirmed",
            entity_id=identity,
            entity_revision=revision,
            correlation_id=identity,
            summary="The owner confirmed the product specification and sale-unit pack size.",
        ),
    )
    result = view(saved)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
