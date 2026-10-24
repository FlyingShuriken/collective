import json
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from collective.adapters.models import ReaderGateway, Task, prepared_text
from collective.adapters.replay import ReplayStore
from collective.api.errors import DomainError
from collective.domain.types import Snapshot
from collective.settings import Settings


class Answer(Snapshot):
    delivery_fee: str | None
    missing: list[str]


TASK = Task("quote", "1", "quote-v1", "Read the supplied quote. Missing fees stay null.")


@pytest.fixture
def store(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "version": "1",
                "synthetic_sources": [prepared_text("Synthetic quote")["sha256"]],
            }
        )
    )
    return ReplayStore(tmp_path)


def config(mode="live"):
    return Settings(
        _env_file=None, model_mode=mode, demo_mode=True, gemini_api_key=SecretStr("test-key")
    )


def response(payload='{"delivery_fee":null,"missing":["delivery_fee"]}', **extra):
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": payload}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 8},
            "responseId": "test-response",
            **extra,
        },
    )


async def test_actual_sdk_sends_schema_and_no_tools_or_storage_credentials():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ReaderGateway(config(), http_client=client)
        source = prepared_text("Synthetic quote") | {
            "token": "must-not-send",
            "source_id": "private",
        }
        result = await gateway.extract(TASK, source, Answer, "fast")
    assert result.value.delivery_fee is None
    body = requests[0]
    assert not body.get("tools")
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"]["additionalProperties"] is False
    assert "must-not-send" not in json.dumps(body) and "private" not in json.dumps(body)
    assert result.attempts[0]["usage"]["prompt_token_count"] == 12


async def test_replay_never_falls_back_and_keys_include_all_versions(store, monkeypatch):
    monkeypatch.setattr(
        "collective.adapters.models.genai.Client",
        lambda *args, **kwargs: pytest.fail("Replay attempted a live client"),
    )
    source = prepared_text("Synthetic quote")
    gateway = ReaderGateway(config("replay"), replay=store)
    with pytest.raises(DomainError) as missing:
        await gateway.extract(TASK, source, Answer)
    assert missing.value.code == "REPLAY_MISS"
    key = gateway.key(TASK, source["sha256"], "fast")
    store.record(
        key,
        '{"delivery_fee":null,"missing":["delivery_fee"]}',
        {},
        provenance="authored_synthetic_replay",
    )
    result = await gateway.extract(TASK, source, Answer)
    assert result.value.missing == ["delivery_fee"] and result.attempts[0]["mode"] == "replay"
    for task, tier in (
        (replace(TASK, schema_version="2"), "fast"),
        (replace(TASK, prompt_version="quote-v2"), "fast"),
        (TASK, "strong"),
    ):
        with pytest.raises(DomainError, match="replay"):
            await gateway.extract(task, source, Answer, tier)


async def test_transport_retries_are_bounded_and_schema_errors_are_not_retried(monkeypatch):
    waits, calls = [], []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr("collective.adapters.models.asyncio.sleep", sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"code": 503, "message": "unavailable"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as failed:
            await ReaderGateway(config(), http_client=client).extract(
                TASK, prepared_text("Synthetic quote"), Answer
            )
    assert failed.value.code == "MODEL_UNAVAILABLE" and len(calls) == 3 and waits == [1, 2]
    assert len(failed.value.attempts) == 3

    calls.clear()

    def invalid(request):
        calls.append(request)
        return response('{"delivery_fee":15,"missing":[],"send":true}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid)) as client:
        with pytest.raises(DomainError) as failed:
            await ReaderGateway(config(), http_client=client).extract(
                TASK, prepared_text("Synthetic quote"), Answer
            )
    assert failed.value.code == "MODEL_SCHEMA_INVALID" and len(calls) == 1
    assert '"send":true' in failed.value.attempts[0]["raw_output"]


async def test_record_replay_validates_the_same_result_and_refuses_unapproved_sources(store):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response())
    ) as client:
        live = await ReaderGateway(config("record"), replay=store, http_client=client).extract(
            TASK, prepared_text("Synthetic quote"), Answer
        )
    cached = await ReaderGateway(config("replay"), replay=store).extract(
        TASK, prepared_text("Synthetic quote"), Answer
    )
    assert live.value == cached.value
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Unapproved private input reached the provider")
        )
    ) as client:
        with pytest.raises(DomainError) as failed:
            await ReaderGateway(config("record"), replay=store, http_client=client).extract(
                TASK, prepared_text("Unapproved private source"), Answer
            )
    assert failed.value.code == "RECORD_NOT_PERMITTED"


async def test_cached_output_is_validated_and_cannot_request_a_tool(store):
    gateway = ReaderGateway(config("replay"), replay=store)
    source = prepared_text("Synthetic quote")
    key = gateway.key(TASK, source["sha256"], "fast")
    store.record(
        key, '{"delivery_fee":15,"missing":[]}', {}, provenance="authored_synthetic_replay"
    )
    with pytest.raises(DomainError) as invalid:
        await gateway.extract(TASK, source, Answer)
    assert invalid.value.code == "MODEL_SCHEMA_INVALID"

    def tool(request):
        return response(
            extra="ignored",
            candidates=[
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "send_message", "args": {}}},
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(tool)) as client:
        with pytest.raises(DomainError) as invalid:
            await ReaderGateway(config(), http_client=client).extract(TASK, source, Answer)
    assert invalid.value.code == "MODEL_TOOL_OUTPUT"
    assert invalid.value.attempts[0]["usage"]["prompt_token_count"] == 12
    assert "send_message" in invalid.value.attempts[0]["raw_output"]


async def test_tampered_prepared_source_stops_before_provider():
    source = prepared_text("Synthetic quote") | {"text": "changed after preparation"}
    with pytest.raises(DomainError) as failed:
        await ReaderGateway(config()).extract(TASK, source, Answer)
    assert failed.value.code == "SOURCE_INTEGRITY_FAILED"


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY"])
async def test_incomplete_response_is_not_a_success_even_with_valid_json(finish):
    def handler(request):
        result = json.loads(response().content)
        result["candidates"][0]["finishReason"] = finish
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as failed:
            await ReaderGateway(config(), http_client=client).extract(
                TASK, prepared_text("Synthetic quote"), Answer
            )
    assert failed.value.code == "MODEL_INCOMPLETE" and len(failed.value.attempts) == 1
    assert failed.value.attempts[0]["raw_output"] and failed.value.attempts[0]["usage"]


async def test_rejected_request_is_not_retried_and_provider_message_is_private():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"code": 400, "message": "private context"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as failed:
            await ReaderGateway(config(), http_client=client).extract(
                TASK, prepared_text("Synthetic quote"), Answer
            )
    assert failed.value.code == "MODEL_REQUEST_REJECTED" and len(calls) == 1
    assert "private context" not in failed.value.message


async def test_tampered_fixture_and_recording_in_production_are_rejected(store):
    gateway = ReaderGateway(config("replay"), replay=store)
    source = prepared_text("Synthetic quote")
    key = gateway.key(TASK, source["sha256"], "fast")
    store.record(key, '{"delivery_fee":null,"missing":[]}', {})
    path = next(p for p in store.root.glob("*.json") if p.name != "manifest.json")
    fixture = json.loads(path.read_text())
    fixture["raw_output"] = '{"delivery_fee":"0.00","missing":[]}'
    path.write_text(json.dumps(fixture))
    with pytest.raises(DomainError) as failed:
        await gateway.extract(TASK, source, Answer)
    assert failed.value.code == "REPLAY_INVALID"
    settings = config("record").model_copy(update={"demo_mode": False})
    with pytest.raises(DomainError) as failed:
        await ReaderGateway(settings, replay=store).extract(TASK, source, Answer)
    assert failed.value.code == "RECORD_NOT_PERMITTED"


async def test_timeouts_are_bounded_and_keep_attempt_evidence(monkeypatch):
    import asyncio

    timeout = asyncio.timeout
    monkeypatch.setattr("collective.adapters.models.asyncio.timeout", lambda delay: timeout(0.005))
    sleep = asyncio.sleep

    async def fast_backoff(delay):
        await sleep(0)

    monkeypatch.setattr("collective.adapters.models.asyncio.sleep", fast_backoff)

    async def handler(request):
        await sleep(0.1)
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as failed:
            await ReaderGateway(config(), http_client=client).extract(
                TASK, prepared_text("Synthetic quote"), Answer
            )
    assert failed.value.code == "MODEL_UNAVAILABLE"
    assert len(failed.value.attempts) == 3
    assert all(attempt["latency_ms"] >= 0 for attempt in failed.value.attempts)


async def test_prepared_images_send_only_verified_png_bytes_and_page_number():
    import base64
    import hashlib
    from pathlib import Path

    from collective.adapters.replay import digest

    data = (
        Path(__file__).resolve().parents[4] / "fixtures/sources/delivered-invoice.png"
    ).read_bytes()
    page = {"page": 1, "sha256": hashlib.sha256(data).hexdigest(), "width": 1000, "height": 1400}
    normalized = {"version": "1", "text": None, "pages": [page]}
    document = normalized | {
        "sha256": digest(normalized),
        "source_id": "private-source",
        "pages": [page | {"content": data, "object_key": "private-object"}],
    }
    captured = []

    def provider(request):
        captured.append(json.loads(request.content))
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        await ReaderGateway(config(), http_client=client).extract(TASK, document, Answer)
    parts = captured[0]["contents"][0]["parts"]
    inline = next(part["inlineData"] for part in parts if "inlineData" in part)
    assert inline["mime_type"] == "image/png" and base64.urlsafe_b64decode(inline["data"]) == data
    assert "private-source" not in json.dumps(captured) and "private-object" not in json.dumps(
        captured
    )
    broken = document | {"pages": [page | {"content": b"changed"}]}
    with pytest.raises(DomainError) as failed:
        await ReaderGateway(config()).extract(TASK, broken, Answer)
    assert failed.value.code == "SOURCE_INTEGRITY_FAILED"
