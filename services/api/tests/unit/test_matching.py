import hashlib
import json
import math
import shutil

import httpx
import pytest
from pydantic import SecretStr

from collective.adapters.embeddings import (
    DIMENSIONS,
    MODEL,
    REVISION,
    ROOT,
    EmbeddingGateway,
    cosine,
    normalized_text,
    shortlist,
)
from collective.api.errors import DomainError
from collective.settings import Settings


def vector(index=0, sign=1.0):
    values = [0.0] * DIMENSIONS
    values[index] = sign
    return values


def test_normalization_preserves_units_ambiguity_and_multilingual_text():
    assert normalized_text("  ＣＵＰ\t２５０ml \n 80mm ") == "cup 250ml 80mm"
    assert normalized_text(" Cawan KERTAS ") == "cawan kertas"
    assert normalized_text("250毫升纸杯") == "250毫升纸杯"
    assert normalized_text("8oz cup") == "8oz cup"


@pytest.mark.parametrize("value", ["", " \n", "杯" * 667, None, 123])
def test_invalid_normalized_text_is_rejected(value):
    with pytest.raises(ValueError):
        normalized_text(value)


def test_cosine_is_normalized_and_one_item_catalogue_does_not_invent_candidates():
    assert cosine(vector(), vector()) == 1
    assert cosine(vector(), vector(sign=-1.0)) == -1
    assert cosine(vector(), vector(1)) == 0
    assert shortlist(vector(), [("only", vector())]) == (("only", 1.0),)
    assert shortlist(vector(), []) == ()
    ranked = shortlist(vector(), [(key, vector()) for key in "dcba"])
    assert [key for key, _ in ranked] == list("abc")
    with pytest.raises(ValueError, match="unique"):
        shortlist(vector(), [("same", vector()), ("same", vector())])


@pytest.mark.parametrize(
    "bad",
    [
        [0.0] * DIMENSIONS,
        [1.0],
        [math.nan] * DIMENSIONS,
        [math.inf] * DIMENSIONS,
        [True] * DIMENSIONS,
    ],
)
def test_invalid_vectors_cannot_be_scored(bad):
    with pytest.raises(ValueError):
        cosine(vector(), bad)


async def test_recorded_bge_vectors_rank_english_malay_chinese_and_different_packs():
    gateway = EmbeddingGateway(Settings(_env_file=None))
    fixture = json.loads((ROOT / "cases.json").read_text())
    texts = [row["text"] for row in fixture["catalogue"] + fixture["queries"]]
    batch = await gateway.encode(texts, REVISION)
    assert batch.mode == "replay" and batch.model_revision == REVISION
    catalogue = [
        (row["key"], batch.vectors[index]) for index, row in enumerate(fixture["catalogue"])
    ]
    for index, query in enumerate(fixture["queries"], start=len(catalogue)):
        ranked = shortlist(batch.vectors[index], catalogue)
        assert len(ranked) == 3
        if query["expected"]:
            assert ranked[0][0] == query["expected"]
        if query["key"] == "eight-oz":
            assert ranked[0][0] == "paper-250-80" and ranked[0][1] > 0.7
            assert query["expected"] is None  # A high score is not physical compatibility.


async def test_replay_miss_never_falls_back_to_a_live_embedding_call():
    def forbidden(_):
        pytest.fail("A prepared-vector miss must not make a live request")

    gateway = EmbeddingGateway(Settings(_env_file=None), transport=httpx.MockTransport(forbidden))
    with pytest.raises(DomainError) as error:
        await gateway.encode(["An undeclared private supplier product"])
    assert error.value.code == "EMBEDDING_REPLAY_MISS"


async def test_replay_verifies_version_file_hash_and_path(tmp_path):
    for name in ("manifest.json", "vectors.json"):
        shutil.copy(ROOT / name, tmp_path / name)
    gateway = EmbeddingGateway(Settings(_env_file=None), root=tmp_path)
    manifest_path = tmp_path / "manifest.json"
    original = json.loads(manifest_path.read_text())
    for changed in (
        original | {"model_revision": "0" * 40},
        original | {"vectors_path": "../outside.json"},
        original | {"pooling": "mean"},
        original | {"vectors_sha256": "0" * 64},
        {"version": "1"},
    ):
        manifest_path.write_text(json.dumps(changed))
        with pytest.raises(DomainError) as error:
            await gateway.encode(["250ml paper cup with 80mm lid"])
        assert error.value.code == "EMBEDDING_REPLAY_INVALID"
    manifest_path.write_text(json.dumps(original))
    with pytest.raises(DomainError) as error:
        await gateway.encode(["250ml paper cup with 80mm lid"], "0" * 40)
    assert error.value.code == "EMBEDDING_REVISION_MISMATCH"


@pytest.mark.parametrize("inputs", [None, "not a batch", [], [""], ["杯" * 667], ["cup"] * 65])
async def test_batch_bounds_fail_before_external_io(inputs):
    def forbidden(_):
        pytest.fail("Invalid text must not make a request")

    gateway = EmbeddingGateway(
        Settings(
            _env_file=None,
            embedding_mode="local",
            embedding_url="http://127.0.0.1:8086/v1/embeddings",
        ),
        transport=httpx.MockTransport(forbidden),
    )
    with pytest.raises(DomainError) as error:
        await gateway.encode(inputs)
    assert error.value.code == "INVALID_EMBEDDING_INPUT"


async def test_compatible_endpoint_preserves_model_identity_order_and_configured_secret():
    def endpoint(request):
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer synthetic-embedding-token"
        assert body == {
            "model": f"{MODEL}@{REVISION}",
            "input": ["paper cup", "纸杯"],
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "data": [{"index": 1, "embedding": vector(1)}, {"index": 0, "embedding": vector()}],
            },
        )

    gateway = EmbeddingGateway(
        Settings(
            _env_file=None,
            embedding_mode="hosted",
            embedding_url="https://embedding.example/v1/embeddings",
            embedding_api_key=SecretStr("synthetic-embedding-token"),
        ),
        transport=httpx.MockTransport(endpoint),
    )
    batch = await gateway.encode(["PAPER CUP", "纸杯"])
    assert batch.mode == "hosted" and batch.vectors[0][0] == batch.vectors[1][1] == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"model": "unversioned"},
        {"data": []},
        {"data": [{"index": False, "embedding": vector()}]},
        {"data": [{"index": 1, "embedding": vector()}]},
        {"data": [{"index": 0, "embedding": [0.0] * DIMENSIONS}]},
        {"data": [{"index": 0, "embedding": [math.nan] * DIMENSIONS}]},
    ],
)
async def test_endpoint_cannot_substitute_revision_positions_or_invalid_vectors(patch):
    payload = {
        "model": f"{MODEL}@{REVISION}",
        "data": [{"index": 0, "embedding": vector()}],
    } | patch
    gateway = EmbeddingGateway(
        Settings(
            _env_file=None,
            embedding_mode="local",
            embedding_url="http://127.0.0.1:8086/v1/embeddings",
        ),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps(payload).encode())
        ),
    )
    with pytest.raises(DomainError) as error:
        await gateway.encode(["cup"])
    assert error.value.code == "EMBEDDING_INVALID"


@pytest.mark.parametrize(
    "mode,url",
    [
        ("local", "https://other.example/embeddings"),
        ("hosted", "http://other.example/embeddings"),
        ("local", "http://user:password@127.0.0.1/"),
        ("local", "http://127.0.0.1/?token=secret"),
    ],
)
async def test_endpoint_configuration_is_bounded(mode, url):
    def forbidden(_):
        pytest.fail("Disallowed endpoints must fail before a request")

    gateway = EmbeddingGateway(
        Settings(_env_file=None, embedding_mode=mode, embedding_url=url),
        transport=httpx.MockTransport(forbidden),
    )
    with pytest.raises(DomainError) as error:
        await gateway.encode(["cup"])
    assert error.value.code == "EMBEDDING_CONFIGURATION"


async def test_large_response_and_redirect_are_not_followed():
    for response, code in (
        (httpx.Response(200, content=b" " * (4 * 1024**2 + 1)), "EMBEDDING_INVALID"),
        (
            httpx.Response(307, headers={"location": "https://elsewhere.example"}),
            "EMBEDDING_UNAVAILABLE",
        ),
    ):
        gateway = EmbeddingGateway(
            Settings(
                _env_file=None,
                embedding_mode="local",
                embedding_url="http://127.0.0.1:8086/v1/embeddings",
            ),
            transport=httpx.MockTransport(lambda _, response=response: response),
        )
        with pytest.raises(DomainError) as error:
            await gateway.encode(["cup"])
        assert error.value.code == code


def test_recorded_fixture_integrity_is_reproducible():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["source_model"] == "BAAI/bge-m3"
    assert manifest["provenance"] == "recorded_synthetic_local_quantized_inference"
    assert (
        hashlib.sha256((ROOT / manifest["vectors_path"]).read_bytes()).hexdigest()
        == manifest["vectors_sha256"]
    )
