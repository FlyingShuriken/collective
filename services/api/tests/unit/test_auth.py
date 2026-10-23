from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from collective.api.auth import JWTVerifier
from collective.api.errors import DomainError
from collective.settings import Settings


@pytest.fixture
def verifier(monkeypatch):
    private_key = ec.generate_private_key(ec.SECP256R1())
    verifier = JWTVerifier(Settings(_env_file=None, supabase_url="http://127.0.0.1:54321"))
    monkeypatch.setattr(
        verifier.jwks,
        "get_signing_key_from_jwt",
        lambda token: SimpleNamespace(key=private_key.public_key()),
    )
    return verifier, private_key


def claims(**changes):
    now = datetime.now(UTC)
    return {
        "sub": str(uuid4()),
        "role": "authenticated",
        "aud": "authenticated",
        "iss": "http://127.0.0.1:54321/auth/v1",
        "iat": now,
        "exp": now + timedelta(hours=1),
    } | changes


async def test_verifies_signature_and_returns_only_subject_and_masked_token(verifier):
    verifier, key = verifier
    payload = claims(user_metadata={"role": "owner"})
    token = jwt.encode(payload, key, algorithm="ES256", headers={"kid": "test-key"})
    principal = await verifier.verify(token)
    assert str(principal.subject) == payload["sub"]
    assert token not in repr(principal)


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example/auth/v1"},
        {"aud": "other"},
        {"exp": datetime.now(UTC) - timedelta(seconds=1)},
        {"iat": datetime.now(UTC) + timedelta(hours=1)},
        {"role": "service_role"},
        {"sub": "owner-a"},
    ],
)
async def test_rejects_wrong_issuer_audience_time_role_or_subject(verifier, changes):
    verifier, key = verifier
    token = jwt.encode(claims(**changes), key, algorithm="ES256", headers={"kid": "test-key"})
    with pytest.raises(DomainError) as error:
        await verifier.verify(token)
    assert error.value.status == 401


async def test_rejects_forged_signature_missing_expiry_and_algorithm_confusion(verifier):
    verifier, key = verifier
    forged_key = ec.generate_private_key(ec.SECP256R1())
    payload = claims()
    without_expiry = {k: v for k, v in payload.items() if k != "exp"}
    tokens = [
        jwt.encode(payload, forged_key, algorithm="ES256", headers={"kid": "test-key"}),
        jwt.encode(without_expiry, key, algorithm="ES256", headers={"kid": "test-key"}),
        jwt.encode(payload, "x" * 64, algorithm="HS256", headers={"kid": "test-key"}),
        "malformed",
        "x" * 9000,
    ]
    for token in tokens:
        with pytest.raises(DomainError):
            await verifier.verify(token)
