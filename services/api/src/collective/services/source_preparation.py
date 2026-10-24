import hashlib
import io
import json
import math
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from PIL import Image, ImageDraw
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from collective.api.errors import DomainError
from collective.repositories import jobs, sources
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services.permissions import membership


def digest(data):
    return hashlib.sha256(data).hexdigest()


async def upload(database, storage, principal, business, prepared, content, key):
    payload = {"sha256": prepared["sha256"], "mime": prepared["mime"]}
    route = "sources"
    identity = None
    reserved_identity = None
    new = False
    try:
        async with database.transaction("tenant", principal.subject) as connection:
            access = await membership(connection, principal.subject, business)
            previous = await previous_mutation(
                connection, principal.subject, business, route, key, payload
            )
            if previous:
                return await sources.result(
                    connection,
                    await sources.load(connection, business, UUID(previous["source"]["id"])),
                    access,
                    datetime.now(UTC),
                )
            existing = await (
                await connection.execute(
                    """select id from collective.sources
                    where business_id=%s and sha256=%s and status='available'""",
                    (business, prepared["sha256"]),
                )
            ).fetchone()
            if existing:
                row = await sources.load(connection, business, existing["id"])
                if row["preparation_status"] is not None:
                    result = await sources.result(connection, row, access, datetime.now(UTC))
                    await save_mutation(
                        connection,
                        principal.subject,
                        business,
                        route,
                        key,
                        payload,
                        result.model_dump(mode="json"),
                    )
                    return result
                identity, original = row["id"], row["original_object_key"]
            else:
                identity = uuid4()
                reserved_identity = identity
                extension = {
                    "image/png": "png",
                    "image/jpeg": "jpg",
                    "application/pdf": "pdf",
                    "text/plain": "txt",
                }[prepared["mime"]]
                original = f"{business}/{identity}/original.{extension}"
                await connection.execute(
                    """insert into collective.sources
                    (id,business_id,kind,status,original_object_key,text_content,mime_type,
                     size_bytes,page_count,uploaded_by,provenance)
                    values (%s,%s,%s,'uploading',%s,%s,%s,%s,%s,%s,'human')""",
                    (
                        identity,
                        business,
                        prepared["kind"],
                        original,
                        prepared["text"],
                        prepared["mime"],
                        len(content),
                        len(prepared["pages"]) or None,
                        principal.subject,
                    ),
                )
                new = True
        # Transfers and verification deliberately run without a database transaction.
        if original:
            if new:
                await storage.write(original, content, prepared["mime"], token=principal.token)
            await storage.verify(original, prepared["sha256"])
        for page in prepared["pages"]:
            page["object_key"] = f"{business}/{identity}/pages/{page['page']}.png"
            await storage.write(page["object_key"], page["content"], "image/png")
            await storage.verify(page["object_key"], page["sha256"])
        async with database.transaction("tenant", principal.subject) as connection:
            access = await membership(connection, principal.subject, business)
            await connection.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"source:{business}:{prepared['sha256']}",),
            )
            previous = await previous_mutation(
                connection, principal.subject, business, route, key, payload
            )
            if previous:
                await set_context(connection, "coordinator", principal.subject)
                if new:
                    await connection.execute(
                        """update collective.sources set status='failed'
                        where id=%s and status='uploading'""",
                        (identity,),
                    )
                return await sources.result(
                    connection,
                    await sources.load(connection, business, UUID(previous["source"]["id"])),
                    access,
                    datetime.now(UTC),
                )
            other = await (
                await connection.execute(
                    """select id from collective.sources
                    where business_id=%s and sha256=%s and status='available'""",
                    (business, prepared["sha256"]),
                )
            ).fetchone()
            if other and other["id"] != identity:
                await set_context(connection, "coordinator", principal.subject)
                await connection.execute(
                    """update collective.sources set status='failed'
                    where id=%s and status='uploading'""",
                    (identity,),
                )
                identity = other["id"]
            else:
                await sources.load(connection, business, identity, lock=True)
                if new:
                    await connection.execute(
                        """update collective.sources set status='available',sha256=%s
                        where business_id=%s and id=%s""",
                        (prepared["sha256"], business, identity),
                    )
                await set_context(connection, "coordinator", principal.subject)
                existing = await sources.load(connection, business, identity)
                if existing["preparation_status"] is None:
                    for page in prepared["pages"]:
                        await connection.execute(
                            """insert into collective.source_pages
                            (business_id,source_id,page,object_key,width,height,transformations)
                            values (%s,%s,%s,%s,%s,%s,%s)""",
                            (
                                business,
                                identity,
                                page["page"],
                                page["object_key"],
                                page["width"],
                                page["height"],
                                Jsonb(page["transformations"] | {"sha256": page["sha256"]}),
                            ),
                        )
                    job = await jobs.enqueue(
                        connection,
                        business=business,
                        kind="intake",
                        revision=1,
                        key=f"intake:{identity}:{prepared['sha256']}:schema-1:intake-v1",
                        payload={
                            "source_id": str(identity),
                            "source_sha256": prepared["sha256"],
                            "schema_version": "1",
                            "prompt_version": "intake-v1",
                        },
                        due=datetime.now(UTC),
                        waiting_review=True,
                    )
                    await connection.execute(
                        """insert into collective.source_preparations
                        (business_id,source_id,ingestion_job_id) values (%s,%s,%s)""",
                        (business, identity, job["id"]),
                    )
                    await append_event(
                        connection,
                        Event(
                            business_id=business,
                            actor_id=principal.subject,
                            actor_kind=access.role,
                            event_type="source_uploaded",
                            entity_id=identity,
                            entity_revision=1,
                            correlation_id=identity,
                            summary="A private source was uploaded. "
                            "Source review is required before extraction.",
                        ),
                    )
            result = await sources.result(
                connection,
                await sources.load(connection, business, identity),
                access,
                datetime.now(UTC),
            )
            await save_mutation(
                connection,
                principal.subject,
                business,
                route,
                key,
                payload,
                result.model_dump(mode="json"),
            )
            return result
    except Exception:
        if reserved_identity is not None:
            async with database.transaction("coordinator", principal.subject) as connection:
                await connection.execute(
                    """update collective.sources set status='failed'
                    where business_id=%s and id=%s and uploaded_by=%s and status='uploading'""",
                    (business, reserved_identity, principal.subject),
                )
        raise


def redact_text(text, known):
    spans = []
    patterns = [
        ("email", r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
        (
            "account",
            r"(?im)(?:bank\s*account|account\s*(?:no\.?|number)|akaun\s*bank|银行账号)"
            r"\s*[:：]?\s*([\d -]{6,})",
        ),
        ("phone", r"(?im)(?:phone|tel(?:ephone)?|telefon|电话)\s*[:：]?\s*(\+?[\d ()-]{7,})"),
    ]
    for label, pattern in patterns:
        for match in re.finditer(pattern, text):
            spans.append((*match.span(match.lastindex or 0), label))
    for value in known:
        for match in re.finditer(re.escape(value), text):
            spans.append((*match.span(), "reviewed_value"))
    characters = list(text)
    for start, end, _ in spans:
        for index in range(start, end):
            if characters[index] not in "\r\n\t":
                characters[index] = "█"
    return "".join(characters), [
        {"start": a, "end": b, "kind": c} for a, b, c in sorted(set(spans))
    ]


def mask_page(content, regions):
    with Image.open(io.BytesIO(content)) as image:
        image = image.convert("RGB")
        draw = ImageDraw.Draw(image)
        applied = []
        for region in regions:
            left, top, right, bottom = region.bbox
            pixels = (
                math.floor(left * image.width / 1000),
                math.floor(top * image.height / 1000),
                math.ceil(right * image.width / 1000),
                math.ceil(bottom * image.height / 1000),
            )
            draw.rectangle((pixels[0], pixels[1], pixels[2] - 1, pixels[3] - 1), fill="black")
            applied.append({"page": region.page, "bbox": list(region.bbox), "pixels": list(pixels)})
        image.info.clear()
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue(), applied


async def prepare(database, storage, principal, business, identity, request, key):
    payload = request.model_dump(mode="json")
    review_hash = digest(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    route = f"sources/{identity}/prepare"
    async with database.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business)
        row = await sources.load(connection, business, identity)
        sources.require_view(row, access)
        if row["sha256"] != request.source_sha256:
            raise DomainError("STALE_SOURCE", "Review the saved source before preparing it.", 409)
        if row["preparation_status"] == "ready":
            if row["review_hash"] != review_hash:
                raise DomainError(
                    "SOURCE_ALREADY_PREPARED", "The reviewed preparation is immutable.", 409
                )
            return await sources.result(connection, row, access, datetime.now(UTC))
        if row["preparation_status"] is None:
            raise DomainError(
                "SOURCE_NOT_PREPARED", "Upload this source to prepare its pages.", 409
            )
        pages = await sources.pages(connection, row)
        if any(region.page not in {p["page"] for p in pages} for region in request.regions):
            raise DomainError("INVALID_REGION", "Select a region on a saved source page.")
    prefix = f"{business}/{identity}/provider/{review_hash}"
    masked, spans = (
        redact_text(row["text_content"], request.sensitive_text)
        if row["text_content"] is not None
        else (None, [])
    )
    manifest = {
        "version": "1",
        "source_id": str(identity),
        "business_id": str(business),
        "source_sha256": row["sha256"],
        "text": masked,
        "pages": [],
    }
    masks = []
    for page in pages:
        content = await storage.read(page["object_key"], token=principal.token)
        if digest(content) != page["transformations"]["sha256"]:
            raise DomainError(
                "SOURCE_INTEGRITY_FAILED", "The saved page does not match its source.", 503
            )
        data, applied = await run_in_threadpool(
            mask_page, content, [r for r in request.regions if r.page == page["page"]]
        )
        masks.extend(applied)
        page_key = f"{prefix}/page-{page['page']}.png"
        await storage.write(page_key, data, "image/png")
        await storage.verify(page_key, digest(data))
        manifest["pages"].append(
            {
                "page": page["page"],
                "object_key": page_key,
                "sha256": digest(data),
                "width": page["width"],
                "height": page["height"],
            }
        )
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    provider_key = prefix + "/manifest.json"
    await storage.write(provider_key, encoded, "application/json")
    await storage.verify(provider_key, digest(encoded))
    async with database.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business)
        row = await sources.load(connection, business, identity, lock=True)
        sources.require_view(row, access)
        await previous_mutation(connection, principal.subject, business, route, key, payload)
        if row["preparation_status"] == "ready":
            if row["review_hash"] != review_hash:
                raise DomainError(
                    "SOURCE_ALREADY_PREPARED", "The reviewed preparation is immutable.", 409
                )
            return await sources.result(connection, row, access, datetime.now(UTC))
        await set_context(connection, "coordinator", principal.subject)
        await connection.execute(
            "update collective.sources set provider_object_key=%s where business_id=%s and id=%s",
            (provider_key, business, identity),
        )
        for page in manifest["pages"]:
            await connection.execute(
                """update collective.source_pages set provider_object_key=%s
                where business_id=%s and source_id=%s and page=%s""",
                (page["object_key"], business, identity, page["page"]),
            )
        now = datetime.now(UTC)
        await connection.execute(
            """update collective.source_preparations set status='ready',
            review_hash=%s,provider_sha256=%s,reviewed_by=%s,reviewed_at=%s,metadata=%s
            where business_id=%s and source_id=%s""",
            (
                review_hash,
                digest(encoded),
                principal.subject,
                now,
                Jsonb({"text_spans": spans, "regions": masks}),
                business,
                identity,
            ),
        )
        await connection.execute(
            """update collective.jobs set status='queued',next_attempt_at=%s
            where business_id=%s and id=%s and status='waiting_review'""",
            (now, business, row["ingestion_job_id"]),
        )
        await append_event(
            connection,
            Event(
                business_id=business,
                actor_id=principal.subject,
                actor_kind=access.role,
                event_type="source_prepared",
                entity_id=identity,
                entity_revision=1,
                correlation_id=identity,
                summary="Source masking was reviewed and prepared for extraction.",
            ),
        )
        result = await sources.result(
            connection, await sources.load(connection, business, identity), access, now
        )
        await save_mutation(
            connection,
            principal.subject,
            business,
            route,
            key,
            payload,
            result.model_dump(mode="json"),
        )
        return result


async def for_job(database, storage, job):
    async with database.transaction("coordinator", None) as connection:
        saved = await (
            await connection.execute("select * from collective.jobs where id=%s", (job["id"],))
        ).fetchone()
        if (
            saved is None
            or saved["business_id"] != job["business_id"]
            or saved["payload"] != job["payload"]
            or saved["kind"] not in {"intake", "quote_parse"}
        ):
            raise DomainError("SOURCE_JOB_MISMATCH", "The source does not belong to this job.", 409)
        identity = UUID(saved["payload"]["source_id"])
        row = await sources.load(connection, saved["business_id"], identity)
        if (
            row["status"] != "available"
            or row["preparation_status"] != "ready"
            or row["sha256"] != saved["payload"]["source_sha256"]
        ):
            raise DomainError(
                "SOURCE_NOT_PREPARED", "Review source masking before extraction.", 409
            )
        expected = (
            UUID(saved["payload"]["preparation_job_id"])
            if "preparation_job_id" in saved["payload"]
            else saved["id"]
        )
        if row["ingestion_job_id"] != expected:
            raise DomainError("SOURCE_JOB_MISMATCH", "The source does not belong to this job.", 409)
        pages = await sources.pages(connection, row)
    encoded = await storage.read(row["provider_object_key"])
    if digest(encoded) != row["provider_sha256"]:
        raise DomainError(
            "SOURCE_INTEGRITY_FAILED", "The prepared source failed verification.", 503
        )
    manifest = json.loads(encoded)
    if (
        manifest["source_id"] != str(identity)
        or manifest["business_id"] != str(row["business_id"])
        or manifest["source_sha256"] != row["sha256"]
    ):
        raise DomainError(
            "SOURCE_JOB_MISMATCH", "The prepared source does not match this job.", 409
        )
    allowed = {p["page"]: p["provider_object_key"] for p in pages}
    for page in manifest["pages"]:
        if allowed.get(page["page"]) != page["object_key"]:
            raise DomainError(
                "SOURCE_JOB_MISMATCH", "The prepared page does not match this source.", 409
            )
        data = await storage.read(page["object_key"])
        if digest(data) != page["sha256"]:
            raise DomainError(
                "SOURCE_INTEGRITY_FAILED", "The prepared page failed verification.", 503
            )
        page["content"] = data
    # Replay identity follows prepared content, independently of tenant IDs and Storage paths.
    normalized = {
        "version": manifest["version"],
        "text": manifest["text"],
        "pages": [
            {k: page[k] for k in ("page", "sha256", "width", "height")}
            for page in manifest["pages"]
        ],
    }
    return manifest | {
        "sha256": digest(
            json.dumps(
                normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
    }
