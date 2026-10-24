import json
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from collective.adapters.models import ReaderGateway, prepared_text
from collective.adapters.replay import digest
from collective.adapters.storage import Storage
from collective.agents import liaison
from collective.api.errors import DomainError
from collective.repositories import jobs, sources
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services import needs, source_preparation
from collective.services.permissions import membership


async def supplier(connection, business, identity):
    row = await (
        await connection.execute(
            """select s.id,s.name,a.contact from collective.business_suppliers a
        join collective.suppliers s on s.id=a.supplier_id
        where a.business_id=%s and a.supplier_id=%s""",
            (business, identity),
        )
    ).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    return row


async def enqueue(connection, business, kind, revision, base, payload, command_key, as_of):
    existing = await (
        await connection.execute(
            """select * from collective.jobs where business_id=%s and kind=%s
        and (dedup_key=%s or starts_with(dedup_key,%s)) order by created_at desc,id desc limit 1""",
            (business, kind, base, base + ":retry:"),
        )
    ).fetchone()
    if existing and existing["status"] != "failed":
        return existing
    key = base if existing is None else base + ":retry:" + command_key
    return await jobs.enqueue(
        connection,
        business=business,
        kind=kind,
        revision=revision,
        key=key,
        payload=payload,
        due=as_of,
    )


async def request_quote(connection, subject, business, body, key, as_of):
    access = await membership(connection, subject, business)
    route, payload = "quote-proposals", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        from collective.api.dto import JobStatus

        return JobStatus.model_validate(prior)
    row = await sources.load(connection, business, body.source_id, lock=True)
    sources.require_view(row, access)
    if row["sha256"] != body.source_sha256 or row["preparation_status"] != "ready":
        raise DomainError(
            "SOURCE_NOT_PREPARED", "Review and prepare the current source first.", 409
        )
    await supplier(connection, business, body.supplier_id)
    spec = await (
        await connection.execute(
            "select spec_id from collective.spec_revisions where spec_id=%s and revision=%s",
            (body.spec_id, body.spec_revision),
        )
    ).fetchone()
    if spec is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    existing = await (
        await connection.execute(
            """select payload from collective.jobs where business_id=%s and kind='quote_parse'
        and payload->>'source_id'=%s order by created_at limit 1""",
            (business, str(body.source_id)),
        )
    ).fetchone()
    context = {k: payload[k] for k in ("supplier_id", "spec_id", "spec_revision")}
    if existing and existing["payload"]["context"] != context:
        raise DomainError(
            "SOURCE_CONTEXT_CONFLICT", "This source already has a quote context.", 409
        )
    await set_context(connection, "coordinator", subject)
    job = await enqueue(
        connection,
        business,
        "quote_parse",
        1,
        f"quote:{business}:{body.source_id}:{liaison.QUOTE.schema_version}:{liaison.QUOTE.prompt_version}",
        {
            "source_id": str(body.source_id),
            "source_sha256": row["sha256"],
            "preparation_job_id": str(row["ingestion_job_id"]),
            "context": context,
            "requester": str(subject),
            "schema_version": liaison.QUOTE.schema_version,
            "prompt_version": liaison.QUOTE.prompt_version,
        },
        key,
        as_of,
    )
    result = jobs.public_status(job, as_of)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind=access.role,
            event_type="quote_requested",
            entity_id=job["id"],
            entity_revision=1,
            correlation_id=job["id"],
            summary="A reviewer requested a supplier quote proposal.",
        ),
    )
    return result


async def draft_context(connection, business, body):
    target = await supplier(connection, business, body.supplier_id)
    if body.kind == "rfq":
        row = await needs.load_need(connection, business, body.reference_id)
        if row["revision"] != body.expected_revision or row["state"] not in {
            "selected",
            "reserved",
        }:
            raise DomainError(
                "STALE_REVISION", "Review the selected need before requesting a draft.", 409
            )
        spec = await (
            await connection.execute(
                """select s.name,r.attributes from collective.canonical_specs s
            join collective.spec_revisions r on r.spec_id=s.id
            where s.id=%s and r.revision=%s""",
                (row["spec_id"], row["spec_revision"]),
            )
        ).fetchone()
        facts = {
            "kind": "rfq",
            "specification": spec["name"],
            "attributes": spec["attributes"],
            "quantity": str(row["quantity"]),
            "base_unit": row["base_unit"],
            "arrival_start": row["arrival_start"].isoformat(),
            "arrival_end": row["arrival_end"].isoformat(),
            "delivery_mode": row["delivery_mode"],
            "delivery_location": row["delivery_location"],
        }
    else:
        row = await (
            await connection.execute(
                """select c.*,po.supplier_id,po.snapshot,p.document_number
            from collective.invoice_checks c
            join collective.purchase_orders po on (po.business_id,po.id)=(c.business_id,c.po_id)
            join collective.purchases p on (p.business_id,p.id,p.revision)=
              (c.business_id,c.purchase_id,c.purchase_revision)
            where c.business_id=%s and c.id=%s and p.sealed and p.purpose='delivered'
              and p.supplier_id=po.supplier_id""",
                (business, body.reference_id),
            )
        ).fetchone()
        if row is None or row["supplier_id"] != body.supplier_id:
            raise DomainError("NOT_FOUND", "Resource not found.", 404)
        if row["revision"] != body.expected_revision or row["status"] != "flagged":
            raise DomainError("STALE_REVISION", "Review the flagged invoice check first.", 409)
        facts = {
            "kind": "invoice_query",
            "document_number": row["document_number"],
            "differences": row["differences"],
            "issued_terms": row["snapshot"]["terms"],
        }
    business_row = await (
        await connection.execute(
            "select locale from collective.businesses where id=%s",
            (business,),
        )
    ).fetchone()
    from collective.repositories.rules import accuracy_warnings

    facts |= {"supplier": target["name"], "language": business_row["locale"]}
    warnings = await accuracy_warnings(connection, business, body.supplier_id)
    if warnings:
        facts["accuracy_warnings"] = warnings
    return json.loads(json.dumps(facts, default=str)), target["contact"] or target["name"]


async def request_draft(connection, subject, business, body, key, as_of):
    access = await membership(connection, subject, business, owner=True)
    route, payload = "drafts", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        from collective.api.dto import JobStatus

        return JobStatus.model_validate(prior)
    # Serialize matching requests; domain rows are read again before committing model output.
    lock = f"draft:{business}:{digest(payload)}"
    await connection.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))", (lock,))
    facts, recipient = await draft_context(connection, business, body)
    fingerprint = digest({"request": payload, "facts": facts, "recipient": recipient})
    base = (
        f"draft:{business}:{fingerprint}:{liaison.DRAFT.schema_version}:"
        f"{liaison.DRAFT.prompt_version}"
    )
    await set_context(connection, "coordinator", subject)
    job = await enqueue(
        connection,
        business,
        "draft",
        body.expected_revision,
        base,
        {
            "request": payload,
            "facts": facts,
            "recipient": recipient,
            "requester": str(subject),
            "schema_version": liaison.DRAFT.schema_version,
            "prompt_version": liaison.DRAFT.prompt_version,
        },
        key,
        as_of,
    )
    result = jobs.public_status(job, as_of)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind=access.role,
            event_type="draft_requested",
            entity_id=job["id"],
            entity_revision=1,
            correlation_id=job["id"],
            summary="An owner requested a supplier message draft.",
        ),
    )
    return result


async def reading(database, job, gateway, operation, document):
    try:
        result = await operation(gateway, document)
    except DomainError as exc:
        async with database.transaction("coordinator", None) as connection:
            await jobs.record_attempts(connection, job, getattr(exc, "attempts", ()))
        raise
    async with database.transaction("coordinator", None) as connection:
        await jobs.record_attempts(
            connection, job, result.attempts, result.value.model_dump(mode="json")
        )
    return result


async def handle_quote(database, job, clock, *, settings, gateway=None):
    require_version(job, liaison.QUOTE)
    subject, business = UUID(job["payload"]["requester"]), job["business_id"]
    async with database.transaction("coordinator", None) as connection:
        access = await membership(connection, subject, business)
        sources.require_view(
            await sources.load(connection, business, UUID(job["payload"]["source_id"])), access
        )
    document = await source_preparation.for_job(database, Storage(settings), job)
    result = await reading(
        database, job, gateway or ReaderGateway(settings), liaison.parse, document
    )
    anchors, invalid = liaison.evidence(result.value, document)
    flags = liaison.review_flags(result.value) + (["evidence:review"] if invalid else [])
    async with database.transaction("coordinator", None) as connection:
        await jobs.hold_claim(connection, job, clock.now())
        access = await membership(connection, subject, business)
        source = await sources.load(connection, business, UUID(document["source_id"]), lock=True)
        sources.require_view(source, access)
        existing = await (
            await connection.execute(
                """select id,status from collective.proposals where business_id=%s and source_id=%s
            and kind='quote'""",
                (business, source["id"]),
            )
        ).fetchone()
        identity = existing["id"] if existing else uuid4()
        if existing is None:
            await connection.execute(
                """insert into collective.proposals(id,business_id,source_id,kind,current_revision)
                values (%s,%s,%s,'quote',1)""",
                (identity, business, source["id"]),
            )
            await connection.execute(
                """insert into collective.proposal_revisions
                (business_id,proposal_id,revision,payload,evidence,flags,agent,model,prompt_version,
                 schema_version) values (%s,%s,1,%s,%s,%s,'liaison',%s,%s,%s)""",
                (
                    business,
                    identity,
                    Jsonb(
                        {
                            "reading": result.value.model_dump(mode="json"),
                            "context": job["payload"]["context"],
                            "interpretation_mode": result.attempts[-1]["mode"],
                        }
                    ),
                    Jsonb(anchors),
                    Jsonb(flags),
                    result.attempts[-1]["model"],
                    liaison.QUOTE.prompt_version,
                    liaison.QUOTE.schema_version,
                ),
            )
            await append_event(
                connection,
                Event(
                    business_id=business,
                    actor_id=None,
                    actor_kind="system",
                    event_type="quote_proposed",
                    entity_id=identity,
                    entity_revision=1,
                    correlation_id=job["id"],
                    summary="A supplier quote proposal needs human confirmation.",
                ),
            )
        await jobs.complete(
            connection,
            job,
            {
                "proposal_id": str(identity),
                "state": existing["status"] if existing else "pending_review",
            },
            clock.now(),
        )


async def handle_draft(database, job, clock, *, settings, gateway=None):
    from collective.api.dto import RequestDraft

    require_version(job, liaison.DRAFT)
    subject, business = UUID(job["payload"]["requester"]), job["business_id"]
    body = RequestDraft.model_validate(job["payload"]["request"])
    async with database.transaction("coordinator", None) as connection:
        await membership(connection, subject, business, owner=True)
        facts, recipient = await draft_context(connection, business, body)
    if facts != job["payload"]["facts"] or recipient != job["payload"]["recipient"]:
        raise DomainError("STALE_CONTEXT", "Request a new draft from the current facts.", 409)
    document = prepared_text(
        json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    result = await reading(
        database, job, gateway or ReaderGateway(settings), liaison.draft, document
    )
    async with database.transaction("coordinator", None) as connection:
        await membership(connection, subject, business, owner=True)
        facts, recipient = await draft_context(connection, business, body)
        if facts != job["payload"]["facts"] or recipient != job["payload"]["recipient"]:
            raise DomainError("STALE_CONTEXT", "Request a new draft from the current facts.", 409)
        await jobs.hold_claim(connection, job, clock.now())
        await connection.execute(
            """insert into collective.conversations(id,business_id,supplier_id)
            values (%s,%s,%s) on conflict(business_id,supplier_id) do nothing""",
            (uuid4(), business, body.supplier_id),
        )
        conversation = await (
            await connection.execute(
                "select id from collective.conversations where business_id=%s and supplier_id=%s",
                (business, body.supplier_id),
            )
        ).fetchone()
        identity = uuid4()
        await connection.execute(
            """insert into collective.message_drafts
            (id,business_id,conversation_id,kind,text_content,recipient,input_context)
            values (%s,%s,%s,%s,%s,%s,%s)""",
            (
                identity,
                business,
                conversation["id"],
                body.kind,
                result.value.text_content,
                recipient,
                Jsonb(
                    {
                        "facts": facts,
                        "request": job["payload"]["request"],
                        "interpretation_mode": result.attempts[-1]["mode"],
                        "model_attempt_ids": [attempt["id"] for attempt in result.attempts],
                    }
                ),
            ),
        )
        await append_event(
            connection,
            Event(
                business_id=business,
                actor_id=None,
                actor_kind="system",
                event_type="draft_proposed",
                entity_id=identity,
                entity_revision=1,
                correlation_id=job["id"],
                summary="A supplier message draft needs owner review and Send.",
            ),
        )
        await jobs.complete(
            connection, job, {"draft_id": str(identity), "state": "draft"}, clock.now()
        )


def require_version(job, task):
    if (
        job["payload"].get("schema_version") != task.schema_version
        or job["payload"].get("prompt_version") != task.prompt_version
    ):
        raise DomainError(
            "STALE_AGENT_VERSION", "Request a reading with the current schema and prompt.", 409
        )
