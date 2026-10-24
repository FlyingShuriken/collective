from typing import Annotated, Literal
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from collective.adapters.storage import Storage
from collective.api.auth import DatabaseDependency, SignedIn
from collective.api.dto import (
    JobStatus,
    MaskRegion,
    PrepareSource,
    RetryIntake,
    SourcePageList,
    SourceResult,
)
from collective.api.errors import DomainError
from collective.api.need_routes import CommandKey
from collective.domain.clock import SystemClock
from collective.repositories import sources
from collective.services import intake_jobs, source_preparation
from collective.services.permissions import membership
from collective.services.source_rendering import MAX_BYTES, crop_page, inspect_source

router = APIRouter(prefix="/v1/businesses/{business_id}", tags=["sources"])


class BodyTooLarge(MultiPartException):
    pass


@router.post(
    "/sources",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "text": {"type": "string", "maxLength": 16000},
                        },
                    }
                }
            },
        }
    },
)
async def upload(
    business_id: UUID,
    request: Request,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> SourceResult:
    async with db.transaction("tenant", principal.subject) as connection:
        await membership(connection, principal.subject, business_id)

    async def bounded():
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BYTES + 65536:
                raise BodyTooLarge("Source size limit")
            yield chunk

    try:
        if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            raw = b"".join([chunk async for chunk in bounded()])
            try:
                form = FormData(
                    parse_qsl(
                        raw.decode("utf-8"),
                        max_num_fields=1,
                        encoding="utf-8",
                        errors="strict",
                        strict_parsing=True,
                    )
                )
            except (ValueError, UnicodeDecodeError) as exc:
                raise DomainError("INVALID_REQUEST", "Use one bounded UTF-8 text field.") from exc
        else:
            form = await MultiPartParser(
                request.headers, bounded(), max_files=1, max_fields=1, max_part_size=65536
            ).parse()
    except MultiPartException as exc:
        raise DomainError(
            "SOURCE_SIZE_LIMIT" if isinstance(exc, BodyTooLarge) else "INVALID_SOURCE",
            "Use one supported file or a bounded text source.",
            413 if isinstance(exc, BodyTooLarge) else 422,
        ) from exc
    try:
        if any(k not in {"file", "text"} for k, _ in form.multi_items()) or any(
            len(form.getlist(k)) > 1 for k in form
        ):
            raise DomainError("INVALID_REQUEST", "Use one file or text field.")
        file, text = form.get("file"), form.get("text")
        if isinstance(file, UploadFile) and not text:
            content = await file.read(MAX_BYTES + 1)
            mime = file.content_type or "application/octet-stream"
        elif isinstance(text, str) and not file:
            content, mime = text.encode("utf-8"), "text/plain"
        else:
            raise DomainError("INVALID_REQUEST", "Provide one file or text source.")
        async with request.app.state.source_slots:
            prepared = await run_in_threadpool(inspect_source, content, mime)
        return await source_preparation.upload(
            db, Storage(request.app.state.settings), principal, business_id, prepared, content, key
        )
    finally:
        await form.close()


@router.get("/sources")
async def listing(
    business_id: UUID,
    principal: SignedIn,
    db: DatabaseDependency,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> SourcePageList:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        parameters = [business_id]
        query = "select id from collective.sources where business_id=%s"
        if cursor:
            row = await sources.load(connection, business_id, cursor)
            query += " and (created_at,id)<(%s,%s)"
            parameters.extend((row["created_at"], cursor))
        query += " order by created_at desc,id desc limit %s"
        parameters.append(limit + 1)
        rows = await (await connection.execute(query, parameters)).fetchall()
        values = []
        for row in rows[:limit]:
            values.append(
                (
                    await sources.result(
                        connection,
                        await sources.load(connection, business_id, row["id"]),
                        access,
                        SystemClock().now(),
                    )
                ).source
            )
        return SourcePageList(
            items=tuple(values), next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None
        )


@router.get("/sources/{source_id}")
async def source(
    business_id: UUID, source_id: UUID, principal: SignedIn, db: DatabaseDependency
) -> SourceResult:
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        return await sources.result(
            connection,
            await sources.load(connection, business_id, source_id),
            access,
            SystemClock().now(),
        )


@router.post("/sources/{source_id}/prepare")
async def prepare(
    business_id: UUID,
    source_id: UUID,
    body: PrepareSource,
    key: CommandKey,
    request: Request,
    principal: SignedIn,
    db: DatabaseDependency,
) -> SourceResult:
    return await source_preparation.prepare(
        db, Storage(request.app.state.settings), principal, business_id, source_id, body, key
    )


@router.get("/sources/{source_id}/view")
async def view(
    business_id: UUID,
    source_id: UUID,
    request: Request,
    principal: SignedIn,
    db: DatabaseDependency,
    page: int = Query(default=1, ge=1, le=10),
    variant: Literal["page", "original", "provider"] = "page",
    bbox: Annotated[list[int] | None, Query(min_length=4, max_length=4)] = None,
):
    async with db.transaction("tenant", principal.subject) as connection:
        access = await membership(connection, principal.subject, business_id)
        row = await sources.load(connection, business_id, source_id)
        sources.require_view(row, access)
        pages = await sources.pages(connection, row)
    storage = Storage(request.app.state.settings)
    headers = {"X-Content-Type-Options": "nosniff"}
    if variant == "original":
        content = (
            await storage.read(row["original_object_key"], token=principal.token)
            if row["original_object_key"]
            else row["text_content"].encode()
        )
        if source_preparation.digest(content) != row["sha256"]:
            raise DomainError(
                "SOURCE_INTEGRITY_FAILED", "The saved original failed verification.", 503
            )
        extension = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "application/pdf": "pdf",
            "text/plain": "txt",
        }.get(row["mime_type"], "bin")
        headers["Content-Disposition"] = f'attachment; filename="source-{source_id}.{extension}"'
        return Response(content, media_type=row["mime_type"], headers=headers)
    manifest = None
    if variant == "provider":
        if row["preparation_status"] != "ready":
            raise DomainError(
                "SOURCE_NOT_PREPARED", "Review source masking before extraction.", 409
            )
        encoded = await storage.read(row["provider_object_key"], token=principal.token)
        if source_preparation.digest(encoded) != row["provider_sha256"]:
            raise DomainError(
                "SOURCE_INTEGRITY_FAILED", "The prepared source failed verification.", 503
            )
        import json

        manifest = json.loads(encoded)
    if row["kind"] in {"text", "supplier_reply"}:
        if bbox:
            raise DomainError("INVALID_REGION", "Text sources use character spans.")
        return Response(
            manifest["text"] if manifest else row["text_content"],
            media_type="text/plain",
            headers=headers,
        )
    own = next((p for p in pages if p["page"] == page), None)
    if own is None:
        raise DomainError("NOT_FOUND", "Page not found.", 404)
    target = next((p for p in manifest["pages"] if p["page"] == page), None) if manifest else None
    if manifest and (target is None or target["object_key"] != own["provider_object_key"]):
        raise DomainError(
            "SOURCE_INTEGRITY_FAILED", "The prepared page does not match its source.", 503
        )
    content = await storage.read(
        target["object_key"] if target else own["object_key"], token=principal.token
    )
    if source_preparation.digest(content) != (
        target["sha256"] if target else own["transformations"]["sha256"]
    ):
        raise DomainError("SOURCE_INTEGRITY_FAILED", "The saved page failed verification.", 503)
    if bbox:
        try:
            region = MaskRegion(page=page, bbox=tuple(bbox))
        except ValueError as exc:
            raise DomainError("INVALID_REGION", "Use valid normalized source bounds.") from exc
        content = await run_in_threadpool(crop_page, content, region.bbox)
    return Response(content, media_type="image/png", headers=headers)


@router.post("/sources/{source_id}/intake")
async def intake(
    business_id: UUID,
    source_id: UUID,
    body: RetryIntake,
    key: CommandKey,
    principal: SignedIn,
    db: DatabaseDependency,
) -> JobStatus:
    async with db.transaction("tenant", principal.subject) as connection:
        return await intake_jobs.request(
            connection, principal.subject, business_id, source_id, body, key, SystemClock().now()
        )
