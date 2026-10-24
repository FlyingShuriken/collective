from collective.api.dto import HiddenSource, SourceResult, SourceSummary, VisibleSource
from collective.api.errors import DomainError
from collective.repositories.jobs import public_status


async def load(connection, business, source, *, lock=False):
    query = """select s.*,p.status as preparation_status,p.ingestion_job_id,p.review_hash,
        p.provider_sha256,p.metadata as preparation_metadata
        from collective.sources s left join collective.source_preparations p
        on (p.business_id,p.source_id)=(s.business_id,s.id)
        where s.business_id=%s and s.id=%s"""
    if lock:
        query += " for update of s"
    row = await (await connection.execute(query, (business, source))).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    return row


async def pages(connection, row):
    return await (
        await connection.execute(
            """select * from collective.source_pages where business_id=%s and source_id=%s
        order by page""",
            (row["business_id"], row["id"]),
        )
    ).fetchall()


async def result(connection, row, access, as_of):
    summary = {key: row[key] for key in SourceSummary.model_fields}
    if access.can_view_prices or not row["price_bearing"]:
        source = VisibleSource(
            **summary,
            sha256=row["sha256"],
            pages=tuple(
                {key: p[key] for key in ("page", "width", "height", "transformations")}
                for p in await pages(connection, row)
            ),
        )
    else:
        source = HiddenSource(**summary)
    job = None
    if row["ingestion_job_id"]:
        record = await (
            await connection.execute(
                "select * from collective.jobs where business_id=%s and id=%s",
                (row["business_id"], row["ingestion_job_id"]),
            )
        ).fetchone()
        job = public_status(record, as_of)
    return SourceResult(source=source, job=job)


def require_view(row, access):
    if not access.can_view_prices and row["price_bearing"]:
        raise DomainError(
            "SOURCE_RESTRICTED", "This account cannot view price-bearing sources.", 403
        )
    if row["status"] != "available":
        raise DomainError("SOURCE_UNAVAILABLE", "This source is not available.", 404)
