"""Pinned BGE-M3 dense retrieval. Similarity never grants mapping authority."""

import asyncio
import hashlib
import json
import math
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import Field, TypeAdapter, model_validator

from collective.api.errors import DomainError
from collective.domain.types import Snapshot

MODEL = "gpustack/bge-m3-GGUF"
REVISION = "2d48f1737679ad900d5c26c5aad5410e9c70fdca"
WEIGHTS_SHA256 = "6d39681b26c61279ac1f82db35a04a05009e94c415b51c858ff571489a82fc06"
DIMENSIONS = 1024
ROOT = Path(__file__).resolve().parents[5] / "fixtures/matching"
Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Vector = Annotated[tuple[Coordinate, ...], Field(min_length=DIMENSIONS, max_length=DIMENSIONS)]
VECTORS = TypeAdapter(tuple[Vector, ...])
VECTOR = TypeAdapter(Vector)


def normalized_text(value):
    if not isinstance(value, str):
        raise ValueError("Embedding input must be text")
    text = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not text or len(text.encode("utf-8")) > 2000:
        raise ValueError("Embedding text must contain 1–2000 UTF-8 bytes")
    return text


def unit_vector(vector):
    vector = VECTOR.validate_python(vector)
    norm = math.hypot(*vector)
    if norm == 0 or not math.isfinite(norm):
        raise ValueError("Embedding vector must have a finite nonzero norm")
    return tuple(value / norm for value in vector)


def cosine(left, right):
    left, right = unit_vector(left), unit_vector(right)
    return max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(left, right, strict=True))))


def shortlist(query, catalogue):
    unit_vector(query)
    if len({key for key, _ in catalogue}) != len(catalogue):
        raise ValueError("Catalogue keys must be unique")
    return tuple(
        sorted(
            ((key, cosine(query, vector)) for key, vector in catalogue),
            key=lambda item: (-item[1], str(item[0])),
        )[:3]
    )


class EmbeddingBatch(Snapshot):
    model_id: Literal["gpustack/bge-m3-GGUF"] = MODEL
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    mode: Literal["local", "hosted", "replay"]
    texts: tuple[str, ...] = Field(min_length=1, max_length=64)
    vectors: tuple[Vector, ...]

    @model_validator(mode="after")
    def valid_batch(self):
        if len(self.texts) != len(self.vectors):
            raise ValueError("Every text requires exactly one vector")
        for vector in self.vectors:
            unit_vector(vector)
        return self


class EmbeddingGateway:
    def __init__(self, settings, *, root=ROOT, transport=None):
        self.settings = settings
        self.root = Path(root)
        self.transport = transport

    async def encode(self, texts, model_revision=None):
        try:
            if not isinstance(texts, list | tuple):
                raise ValueError("Embedding input must be a batch of texts")
            texts = tuple(normalized_text(value) for value in texts)
            if not 1 <= len(texts) <= 64:
                raise ValueError("Embedding batch requires 1–64 texts")
        except ValueError as exc:
            raise DomainError(
                "INVALID_EMBEDDING_INPUT", "Review the product descriptions."
            ) from exc
        revision = model_revision or self.settings.embedding_model_revision
        if revision != self.settings.embedding_model_revision:
            raise DomainError("EMBEDDING_REVISION_MISMATCH", "The embedding version changed.", 409)
        mode = self.settings.embedding_mode
        if mode == "replay":
            vectors = self.replay(texts, revision)
        else:
            vectors = await self.remote(texts, revision)
        try:
            return EmbeddingBatch(model_revision=revision, mode=mode, texts=texts, vectors=vectors)
        except ValueError as exc:
            raise DomainError(
                "EMBEDDING_INVALID", "The embedding response failed validation.", 502
            ) from exc

    def replay(self, texts, revision):
        try:
            manifest = json.loads((self.root / "manifest.json").read_text())
            if (
                manifest["version"],
                manifest["model_id"],
                manifest["model_revision"],
                manifest["weights_sha256"],
                manifest["dimensions"],
                manifest["pooling"],
            ) != (
                "1",
                MODEL,
                revision,
                WEIGHTS_SHA256,
                DIMENSIONS,
                "cls",
            ) or manifest["provenance"] != "recorded_synthetic_local_quantized_inference":
                raise ValueError("Embedding replay metadata differs")
            path = (self.root / manifest["vectors_path"]).resolve()
            if not path.is_relative_to(self.root.resolve()) or path.suffix != ".json":
                raise ValueError("Embedding fixture path leaves its root")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest["vectors_sha256"]:
                raise ValueError("Embedding vectors were modified")
            values = json.loads(raw)
            if not isinstance(values, dict):
                raise ValueError("Embedding replay entries must be keyed by normalized text")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise DomainError(
                "EMBEDDING_REPLAY_INVALID",
                "Prepared embedding data is unavailable or invalid.",
                503,
            ) from exc
        try:
            return tuple(values[text] for text in texts)
        except KeyError as exc:
            # There is no nearest canned vector and no silent live call on a cache miss.
            raise DomainError(
                "EMBEDDING_REPLAY_MISS", "No prepared vector matches this text and version.", 409
            ) from exc

    async def remote(self, texts, revision):
        endpoint = self.settings.embedding_url
        if endpoint is None:
            raise DomainError(
                "EMBEDDING_UNAVAILABLE", "The configured embedding service is unavailable.", 503
            )
        local = endpoint.host.strip("[]") in {"127.0.0.1", "localhost", "::1"}
        if (
            endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or (self.settings.embedding_mode == "local" and not local)
            or (not local and endpoint.scheme != "https")
        ):
            raise DomainError(
                "EMBEDDING_CONFIGURATION", "Review the embedding service configuration.", 503
            )
        headers = {}
        if self.settings.embedding_api_key:
            headers["Authorization"] = (
                "Bearer " + self.settings.embedding_api_key.get_secret_value()
            )
        alias = f"{MODEL}@{revision}"
        try:
            async with (
                asyncio.timeout(30),
                httpx.AsyncClient(
                    timeout=httpx.Timeout(30, connect=5),
                    follow_redirects=False,
                    transport=self.transport,
                ) as client,
            ):
                async with client.stream(
                    "POST",
                    str(endpoint),
                    headers=headers,
                    json={"model": alias, "input": list(texts), "encoding_format": "float"},
                ) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024 * 1024:
                            raise ValueError("Embedding response exceeds its bound")
            data = json.loads(raw)
            rows = data["data"]
            if (
                data["model"] != alias
                or not isinstance(rows, list)
                or len(rows) != len(texts)
                or any(type(row["index"]) is not int for row in rows)
                or sorted(row["index"] for row in rows) != list(range(len(texts)))
            ):
                raise ValueError("Embedding response identity/count differs")
            return VECTORS.validate_python(
                tuple(row["embedding"] for row in sorted(rows, key=lambda row: row["index"]))
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise DomainError(
                "EMBEDDING_UNAVAILABLE",
                "The embedding service could not complete this request.",
                503,
            ) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError(
                "EMBEDDING_INVALID", "The embedding response failed validation.", 502
            ) from exc
