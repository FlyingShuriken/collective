from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from collective.api.errors import DomainError
from collective.repositories.db import Database
from collective.settings import Settings


@dataclass(frozen=True)
class Principal:
    subject: UUID
    token: SecretStr


class JWTVerifier:
    def __init__(self, settings: Settings):
        url = str(settings.supabase_url).rstrip("/")
        self.issuer = settings.supabase_jwt_issuer or url + "/auth/v1"
        self.audience = settings.supabase_jwt_audience
        self.jwks = jwt.PyJWKClient(
            url + "/auth/v1/.well-known/jwks.json",
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=300,
            timeout=5,
        )

    async def verify(self, token: str) -> Principal:
        try:
            if len(token) > 8192:
                raise jwt.InvalidTokenError()
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in {"ES256", "RS256"} or not isinstance(
                header.get("kid"), str
            ):
                raise jwt.InvalidTokenError()
            signing_key = await run_in_threadpool(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256", "RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["sub", "exp", "iat", "iss", "aud"]},
            )
            if claims.get("role") != "authenticated":
                raise jwt.InvalidTokenError()
            return Principal(subject=UUID(claims["sub"]), token=SecretStr(token))
        except jwt.PyJWKClientConnectionError as error:
            raise DomainError(
                "AUTH_UNAVAILABLE", "Sign-in verification is temporarily unavailable.", 503
            ) from error
        except (jwt.PyJWTError, ValueError, TypeError) as error:
            raise DomainError("UNAUTHENTICATED", "A valid sign-in is required.", 401) from error


bearer = HTTPBearer(auto_error=False)


async def authenticated(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None or request.app.state.verifier is None:
        raise DomainError("UNAUTHENTICATED", "A valid sign-in is required.", 401)
    return await request.app.state.verifier.verify(credentials.credentials)


SignedIn = Annotated[Principal, Depends(authenticated)]


def database(request: Request) -> Database:
    if request.app.state.database is None:
        raise DomainError("NOT_READY", "The application is not ready.", 503)
    return request.app.state.database


DatabaseDependency = Annotated[Database, Depends(database)]
