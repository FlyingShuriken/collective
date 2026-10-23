import asyncio
import io
import json
import math
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from collective.settings import Settings
from tests.fixtures import ROOT, scenario
from tests.integration.scenarios import clone_business_data


@pytest.fixture
async def business(access_seed):
    remap = await clone_business_data(access_seed, "a", pool_opt_in=False)
    return remap(scenario()["businesses"][0]["id"])


async def upload(
    client,
    business,
    auth_headers,
    path="delivered-invoice.png",
    *,
    actor="owner-a",
    mime="image/png",
    content=None,
):
    return await client.post(
        f"/v1/businesses/{business}/sources",
        headers=auth_headers(actor) | {"Idempotency-Key": str(uuid4())},
        files={
            "file": (
                path,
                content if content is not None else (ROOT / "fixtures/sources" / path).read_bytes(),
                mime,
            )
        },
    )


async def test_uploaded_source_is_private_and_waits_for_preparation_review(
    api, business, auth_headers
):
    client, database = api
    response = await upload(client, business, auth_headers)
    assert response.status_code == 200
    result = response.json()
    assert result["source"]["status"] == "available"
    assert result["source"]["preparation_status"] == "pending_review"
    assert result["job"]["status"] == "waiting_review"
    source = result["source"]["id"]
    view = f"/v1/businesses/{business}/sources/{source}/view?page=1"
    page = await client.get(view, headers=auth_headers("owner-a"))
    assert page.status_code == 200 and page.headers["content-type"].startswith("image/png")
    for actor, status in (("staff", 403), ("owner-b", 404), ("admin", 404)):
        assert (await client.get(view, headers=auth_headers(actor))).status_code == status
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select original_object_key from collective.sources where id=%s", (source,)
            )
        ).fetchone()
        assert not (
            await conn.execute("select id from collective.purchases where source_id=%s", (source,))
        ).rowcount
    settings = Settings()
    async with httpx.AsyncClient(
        base_url=str(settings.supabase_url), headers={"apikey": settings.supabase_publishable_key}
    ) as storage:
        original = (
            "/storage/v1/object/authenticated/collective-sources/" + row["original_object_key"]
        )
        assert (await storage.get(original, headers=auth_headers("owner-a"))).status_code == 200
        assert (await storage.get(original, headers=auth_headers("staff"))).status_code in {
            400,
            401,
            403,
            404,
        }


@pytest.mark.parametrize(
    "mime,content,status",
    [
        ("image/png", b"not an image", 422),
        ("text/html", b"<script>invalid</script>", 415),
        ("application/pdf", b"%PDF-broken", 422),
        ("image/png", b"\x89PNG\r\n\x1a\n" + b"x" * (10 * 1024 * 1024), 413),
    ],
)
async def test_invalid_source_creates_no_extraction_job(
    api, business, auth_headers, mime, content, status
):
    client, database = api
    response = await upload(client, business, auth_headers, mime=mime, content=content)
    assert response.status_code == status
    async with database.transaction("coordinator", None) as conn:
        assert not (
            await conn.execute(
                "select id from collective.jobs where business_id=%s and kind='intake'", (business,)
            )
        ).rowcount


async def test_text_preparation_masks_known_sensitive_values_and_preserves_offsets(
    api, business, auth_headers
):
    client, _ = api
    text = (
        "Email: owner@example.test\nBank account: 123456789012\nQuantity 1000 cups. Total RM290.00"
    )
    response = await client.post(
        f"/v1/businesses/{business}/sources",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        data={"text": text},
    )
    assert response.status_code == 200
    source = response.json()["source"]
    prepared = await client.post(
        f"/v1/businesses/{business}/sources/{source['id']}/prepare",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={
            "source_sha256": source["sha256"],
            "regions": [],
            "sensitive_text": [],
            "reviewed": True,
        },
    )
    assert (
        prepared.status_code == 200 and prepared.json()["source"]["preparation_status"] == "ready"
    )
    provider = await client.get(
        f"/v1/businesses/{business}/sources/{source['id']}/view?variant=provider",
        headers=auth_headers("owner-a"),
    )
    assert provider.status_code == 200
    assert "owner@example.test" not in provider.text and "123456789012" not in provider.text
    assert len(provider.text) == len(text) and "Total RM290.00" in provider.text


async def test_pdf_and_exif_orientation_are_saved_with_transforms(api, business, auth_headers):
    client, _ = api
    pdf = await upload(
        client, business, auth_headers, "delivered-invoice.pdf", mime="application/pdf"
    )
    assert pdf.status_code == 200
    page = pdf.json()["source"]["pages"][0]
    assert page["height"] == 1800 and page["transformations"]["renderer"] == "pypdfium2:5.14.0"
    image = Image.new("RGB", (120, 240), "white")
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    rotated = await upload(
        client, business, auth_headers, "rotated.jpg", mime="image/jpeg", content=output.getvalue()
    )
    assert rotated.status_code == 200
    normalized = rotated.json()["source"]["pages"][0]
    assert (normalized["width"], normalized["height"]) == (240, 120)
    assert normalized["transformations"]["exif_orientation"] == 6


async def test_page_count_and_dimensions_are_rejected(api, business, auth_headers):
    import pypdfium2 as pdfium

    client, _ = api
    with pdfium.PdfDocument.new() as document:
        for _ in range(11):
            document.new_page(595, 842)
        output = io.BytesIO()
        document.save(output)
    rejected = await upload(
        client,
        business,
        auth_headers,
        "eleven.pdf",
        mime="application/pdf",
        content=output.getvalue(),
    )
    assert rejected.status_code == 422
    tiny = io.BytesIO()
    Image.new("RGB", (1, 1)).save(tiny, format="PNG")
    assert (
        await upload(client, business, auth_headers, content=tiny.getvalue())
    ).status_code == 422


async def test_masks_and_exact_price_crop_preserve_the_private_original(
    api, business, auth_headers
):
    from collective.adapters.storage import Storage
    from collective.api.errors import DomainError
    from collective.services.source_preparation import for_job

    client, database = api
    response = await upload(client, business, auth_headers)
    source = response.json()["source"]
    root = f"/v1/businesses/{business}/sources/{source['id']}"
    bbox = json.loads((ROOT / "fixtures/gold/source-fields.json").read_text())["delivered-invoice"][
        "bbox_normalized"
    ]["lines.0.unit_price"]
    crop = await client.get(
        root + "/view?page=1&" + str(httpx.QueryParams([("bbox", str(v)) for v in bbox])),
        headers=auth_headers("owner-a"),
    )
    assert crop.status_code == 200
    with (
        Image.open(io.BytesIO(crop.content)) as image,
        Image.open(ROOT / "fixtures/sources/delivered-invoice.png") as original,
    ):
        expected = original.crop(
            (
                math.floor(bbox[0] * original.width / 1000),
                math.floor(bbox[1] * original.height / 1000),
                math.ceil(bbox[2] * original.width / 1000),
                math.ceil(bbox[3] * original.height / 1000),
            )
        )
        assert image.tobytes() == expected.convert("RGB").tobytes()
    async with database.transaction("coordinator", None) as conn:
        job = await (
            await conn.execute(
                "select * from collective.jobs where id=%s", (response.json()["job"]["id"],)
            )
        ).fetchone()
    with pytest.raises(DomainError, match="masking"):
        await for_job(database, Storage(Settings()), job)
    body = {
        "source_sha256": source["sha256"],
        "regions": [{"page": 1, "bbox": [0, 0, 200, 200]}],
        "sensitive_text": [],
        "reviewed": True,
    }
    prepared = await client.post(
        root + "/prepare",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json=body,
    )
    assert prepared.status_code == 200 and prepared.json()["job"]["status"] == "queued"
    derived = await client.get(
        root + "/view?variant=provider&page=1", headers=auth_headers("owner-a")
    )
    with Image.open(io.BytesIO(derived.content)) as image:
        assert image.getpixel((10, 10)) == (0, 0, 0)
    original = await client.get(root + "/view?page=1", headers=auth_headers("owner-a"))
    assert original.content != derived.content
    document = await for_job(database, Storage(Settings()), job)
    assert document["pages"][0]["content"] == derived.content
    assert "Authorization" not in document and "token" not in document
    assert (
        await client.post(
            root + "/prepare",
            headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
            json=body | {"regions": []},
        )
    ).status_code == 409
    assert (
        await client.post(
            root + "/prepare",
            headers=auth_headers("staff") | {"Idempotency-Key": str(uuid4())},
            json=body,
        )
    ).status_code == 403


async def test_duplicate_uploads_create_one_available_source_and_job(api, business, auth_headers):
    client, database = api
    responses = await asyncio.gather(*(upload(client, business, auth_headers) for _ in range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json()["source"]["id"] == responses[1].json()["source"]["id"]
    async with database.transaction("coordinator", None) as conn:
        assert (
            await conn.execute(
                "select id from collective.sources where business_id=%s and status='available'",
                (business,),
            )
        ).rowcount == 1
        assert (
            await conn.execute(
                "select id from collective.jobs where business_id=%s and kind='intake'", (business,)
            )
        ).rowcount == 1


async def test_failed_transfer_creates_no_ingestion_job(api, business, auth_headers, monkeypatch):
    from collective.adapters.storage import Storage
    from collective.api.errors import DomainError

    async def fail(*args, **kwargs):
        raise DomainError("SOURCE_TRANSFER_FAILED", "Injected transfer failure.", 503)

    monkeypatch.setattr(Storage, "write", fail)
    client, database = api
    response = await upload(client, business, auth_headers)
    assert response.status_code == 503
    async with database.transaction("coordinator", None) as conn:
        assert (
            await conn.execute(
                "select id from collective.sources where business_id=%s and status='failed'",
                (business,),
            )
        ).rowcount == 1
        assert not (
            await conn.execute(
                "select id from collective.jobs where business_id=%s and kind='intake'", (business,)
            )
        ).rowcount


async def test_invalid_review_cannot_queue_extraction(api, business, auth_headers):
    client, database = api
    source = (await upload(client, business, auth_headers)).json()["source"]
    root = f"/v1/businesses/{business}/sources/{source['id']}"
    body = {"source_sha256": source["sha256"], "reviewed": True}
    for invalid in (
        {"reviewed": False},
        {"source_sha256": "0" * 64},
        {"regions": [{"page": 2, "bbox": [0, 0, 100, 100]}]},
        {"regions": [{"page": 1, "bbox": [200, 0, 100, 100]}]},
    ):
        response = await client.post(
            root + "/prepare",
            headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
            json=body | invalid,
        )
        assert response.status_code in {409, 422}
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select status from collective.source_preparations where source_id=%s",
                (source["id"],),
            )
        ).fetchone()
        assert row["status"] == "pending_review"
    assert (await client.get(root, headers=auth_headers("owner-a"))).json()["job"][
        "status"
    ] == "waiting_review"


async def test_failed_preparation_keeps_review_and_job_pending(
    api, business, auth_headers, monkeypatch
):
    from collective.adapters.storage import Storage
    from collective.api.errors import DomainError

    client, database = api
    source = (await upload(client, business, auth_headers)).json()["source"]

    async def fail(*args, **kwargs):
        raise DomainError("SOURCE_TRANSFER_FAILED", "Injected preparation failure.", 503)

    monkeypatch.setattr(Storage, "verify", fail)
    response = await client.post(
        f"/v1/businesses/{business}/sources/{source['id']}/prepare",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"source_sha256": source["sha256"], "reviewed": True},
    )
    assert response.status_code == 503
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select provider_object_key from collective.sources where id=%s", (source["id"],)
            )
        ).fetchone()
        assert row["provider_object_key"] is None
        assert not (
            await conn.execute(
                """select id from collective.events
                where entity_id=%s and event_type='source_prepared'""",
                (source["id"],),
            )
        ).rowcount
    result = (
        await client.get(
            f"/v1/businesses/{business}/sources/{source['id']}", headers=auth_headers("owner-a")
        )
    ).json()
    assert result["source"]["preparation_status"] == "pending_review"
    assert result["job"]["status"] == "waiting_review"


async def test_prepared_content_identity_is_stable_and_bound_to_its_job(
    api, business, auth_headers, access_seed
):
    from datetime import UTC, datetime

    from collective.adapters.storage import Storage
    from collective.api.errors import DomainError
    from collective.repositories import jobs
    from collective.services.source_preparation import for_job

    client, database = api
    remap = await clone_business_data(access_seed, "a", pool_opt_in=False)
    other = remap(scenario()["businesses"][0]["id"])
    documents = []
    for target in (business, other):
        result = (await upload(client, target, auth_headers)).json()
        source = result["source"]
        response = await client.post(
            f"/v1/businesses/{target}/sources/{source['id']}/prepare",
            headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
            json={"source_sha256": source["sha256"], "reviewed": True},
        )
        assert response.status_code == 200
        async with database.transaction("coordinator", None) as conn:
            job = await (
                await conn.execute(
                    "select * from collective.jobs where id=%s", (result["job"]["id"],)
                )
            ).fetchone()
        documents.append(await for_job(database, Storage(Settings()), job))
    assert documents[0]["source_id"] != documents[1]["source_id"]
    assert documents[0]["sha256"] == documents[1]["sha256"]
    async with database.transaction("coordinator", None) as conn:
        wrong = await jobs.enqueue(
            conn,
            business=other,
            kind="intake",
            revision=1,
            key=str(uuid4()),
            payload=job["payload"],
            due=datetime.now(UTC),
        )
    with pytest.raises(DomainError, match="does not belong"):
        await for_job(database, Storage(Settings()), wrong)
