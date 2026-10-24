from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from collective.adapters.models import ReaderGateway
from collective.adapters.storage import Storage
from collective.agents import intake, liaison
from collective.api.dto import JobStatus, RecordContext
from collective.api.errors import DomainError
from collective.repositories import jobs, sources
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services import source_preparation
from collective.services.permissions import membership


async def request(connection, subject, business, identity, body, key, as_of):
    access = await membership(connection, subject, business)
    route, payload = f"sources/{identity}/intake", body.model_dump(mode="json")
    previous = await previous_mutation(connection, subject, business, route, key, payload)
    if previous:
        return JobStatus.model_validate(previous)
    row = await sources.load(connection, business, identity, lock=True)
    sources.require_view(row, access)
    if row["preparation_status"] != "ready" or row["sha256"] != body.source_sha256:
        raise DomainError("SOURCE_NOT_PREPARED", "Review and prepare the saved source first.", 409)
    existing = await (
        await connection.execute(
            """select * from collective.jobs where business_id=%s and kind='intake'
        and payload->>'source_id'=%s order by created_at desc,id desc limit 1""",
            (business, str(identity)),
        )
    ).fetchone()
    current = (
        existing
        and existing["payload"].get("schema_version") == intake.INTAKE.schema_version
        and existing["payload"].get("prompt_version") == intake.INTAKE.prompt_version
    )
    if current and existing["status"] != "failed":
        job = existing
    else:
        await set_context(connection, "coordinator", subject)
        job = await jobs.enqueue(
            connection,
            business=business,
            kind="intake",
            revision=1,
            key=f"intake:{identity}:{intake.INTAKE.schema_version}:{intake.INTAKE.prompt_version}:{key}",
            payload={
                "source_id": str(identity),
                "source_sha256": row["sha256"],
                "preparation_job_id": str(row["ingestion_job_id"]),
                "requester": str(subject),
                "schema_version": intake.INTAKE.schema_version,
                "prompt_version": intake.INTAKE.prompt_version,
            },
            due=as_of,
        )
        await append_event(
            connection,
            Event(
                business_id=business,
                actor_id=subject,
                actor_kind=access.role,
                event_type="intake_requested",
                entity_id=job["id"],
                entity_revision=1,
                correlation_id=job["id"],
                summary="A reviewer requested source reading. Confirmation remains separate.",
            ),
        )
    result = jobs.public_status(job, as_of)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result


async def authority(connection, job):
    row = await sources.load(connection, job["business_id"], UUID(job["payload"]["source_id"]))
    subject = job["payload"].get("requester") or row["preparation_reviewed_by"]
    if subject is None or row["preparation_status"] != "ready":
        raise DomainError("SOURCE_NOT_PREPARED", "Review source preparation before reading.", 409)
    subject = UUID(str(subject))
    access = await membership(connection, subject, job["business_id"])
    sources.require_view(row, access)
    return row, subject


async def handle(database, job, clock, *, settings, gateway=None):
    if (
        job["payload"].get("schema_version") != intake.INTAKE.schema_version
        or job["payload"].get("prompt_version") != intake.INTAKE.prompt_version
    ):
        raise DomainError(
            "STALE_AGENT_VERSION", "Request reading with the current prompt and schema.", 409
        )
    async with database.transaction("coordinator", None) as connection:
        row, _ = await authority(connection, job)
        existing = await (
            await connection.execute(
                """select id,status from collective.proposals
                where business_id=%s and source_id=%s limit 1""",
                (job["business_id"], row["id"]),
            )
        ).fetchone()
        if existing:
            await jobs.hold_claim(connection, job, clock.now())
            await jobs.complete(
                connection,
                job,
                {"proposal_id": str(existing["id"]), "state": existing["status"]},
                clock.now(),
            )
            return
    document = await source_preparation.for_job(database, Storage(settings), job)

    async def record(attempts, value):
        async with database.transaction("coordinator", None) as connection:
            await jobs.record_attempts(connection, job, attempts, value)

    result = await intake.extract(gateway or ReaderGateway(settings), document, record=record)
    async with database.transaction("coordinator", None) as connection:
        row, _ = await authority(connection, job)
        await jobs.hold_claim(connection, job, clock.now())
        if result.reading.kind == "supplier_reply":
            await jobs.complete(connection, job, {"state": "needs_quote_context"}, clock.now())
            return
        await sources.load(connection, job["business_id"], row["id"], lock=True)
        existing = await (
            await connection.execute(
                """select id,status from collective.proposals
                where business_id=%s and source_id=%s limit 1""",
                (job["business_id"], row["id"]),
            )
        ).fetchone()
        if existing:
            identity, state = existing["id"], existing["status"]
        else:
            identity = uuid4()
            supplier = None
            if result.reading.supplier_name:
                supplier = await (
                    await connection.execute(
                        """select s.id from collective.suppliers s
                    join collective.business_suppliers b on b.supplier_id=s.id
                    where b.business_id=%s and lower(s.name)=lower(%s)""",
                        (job["business_id"], result.reading.supplier_name.strip()),
                    )
                ).fetchone()
            context = RecordContext(supplier_id=supplier["id"] if supplier else None)
            anchors, invalid = liaison.evidence(result.reading, document, fallback_field="document")
            flags = list(result.checks.flags) + (["evidence:review"] if invalid else [])
            chosen = next(
                attempt
                for attempt in reversed(result.attempts)
                if attempt["tier"] == result.selected_tier and not attempt["error_code"]
            )
            # An unreadable source opens a manual-review draft, never an accepted purchase.
            kind = "stock" if result.reading.kind == "stock" else "purchase"
            await connection.execute(
                """insert into collective.proposals(id,business_id,source_id,kind,current_revision)
                values (%s,%s,%s,%s,1)""",
                (identity, job["business_id"], row["id"], kind),
            )
            await connection.execute(
                """insert into collective.proposal_revisions
                (business_id,proposal_id,revision,payload,evidence,flags,agent,model,prompt_version,
                 schema_version) values (%s,%s,1,%s,%s,%s,'intake',%s,%s,%s)""",
                (
                    job["business_id"],
                    identity,
                    Jsonb(
                        {
                            "reading": result.reading.model_dump(mode="json"),
                            "context": context.model_dump(mode="json"),
                            "interpretation_mode": chosen["mode"],
                        }
                    ),
                    Jsonb(anchors),
                    Jsonb(flags),
                    chosen["model"],
                    intake.INTAKE.prompt_version,
                    intake.INTAKE.schema_version,
                ),
            )
            await append_event(
                connection,
                Event(
                    business_id=job["business_id"],
                    actor_id=None,
                    actor_kind="system",
                    event_type="record_proposed",
                    entity_id=identity,
                    entity_revision=1,
                    correlation_id=job["id"],
                    summary="A source reading is ready for human review; no record is confirmed.",
                ),
            )
            state = "unreadable" if result.reading.kind == "unreadable" else "pending_review"
        await jobs.complete(
            connection, job, {"proposal_id": str(identity), "state": state}, clock.now()
        )
