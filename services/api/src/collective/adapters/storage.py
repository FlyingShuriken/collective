import hashlib
from urllib.parse import quote

import httpx

from collective.api.errors import DomainError

BUCKET = "collective-sources"


class Storage:
    def __init__(self, settings):
        if not settings.supabase_url or not settings.supabase_publishable_key:
            raise DomainError("STORAGE_UNAVAILABLE", "Source storage is not configured.", 503)
        self.url = str(settings.supabase_url).rstrip("/")
        self.public_key = settings.supabase_publishable_key
        self.secret = settings.supabase_secret_key

    def headers(self, token=None):
        if token is not None:
            return {
                "apikey": self.public_key,
                "Authorization": "Bearer " + token.get_secret_value(),
            }
        if self.secret is None:
            raise DomainError("STORAGE_UNAVAILABLE", "Source preparation is not configured.", 503)
        return {
            "apikey": self.secret.get_secret_value(),
            "Authorization": "Bearer " + self.secret.get_secret_value(),
        }

    async def write(self, key, content, mime, *, token=None):
        try:
            async with httpx.AsyncClient(base_url=self.url, timeout=20) as client:
                response = await client.post(
                    "/storage/v1/object/" + BUCKET + "/" + quote(key, safe="/"),
                    headers=self.headers(token) | {"Content-Type": mime, "x-upsert": "false"},
                    content=content,
                )
        except httpx.HTTPError as exc:
            raise DomainError(
                "SOURCE_TRANSFER_FAILED", "Source transfer is temporarily unavailable.", 503
            ) from exc
        if response.status_code not in {200, 201}:
            # A retry may find the same immutable object; compare bytes rather than overwriting it.
            if response.status_code in {400, 409}:
                existing = await self.read(key)
                if hashlib.sha256(existing).digest() == hashlib.sha256(content).digest():
                    return
            raise DomainError(
                "SOURCE_TRANSFER_FAILED", "Source transfer failed. Try the upload again.", 503
            )

    async def read(self, key, *, token=None):
        try:
            async with httpx.AsyncClient(base_url=self.url, timeout=20) as client:
                response = await client.get(
                    "/storage/v1/object/authenticated/" + BUCKET + "/" + quote(key, safe="/"),
                    headers=self.headers(token),
                )
        except httpx.HTTPError as exc:
            raise DomainError(
                "SOURCE_UNAVAILABLE", "Source storage is temporarily unavailable.", 503
            ) from exc
        if response.status_code != 200:
            raise DomainError(
                "SOURCE_UNAVAILABLE", "This source is unavailable.", 404 if token else 503
            )
        return response.content

    async def verify(self, key, expected_hash):
        content = await self.read(key)
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise DomainError(
                "SOURCE_INTEGRITY_FAILED", "Source verification failed. Try the upload again.", 503
            )
