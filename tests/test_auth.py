from __future__ import annotations

import base64
import time
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from evidencegate.devauth import create_app
from evidencegate.gateway.auth import AuthError, JWTVerifier, NoAuth, Principal
from evidencegate.gateway.review import MemoryTenants


@pytest.fixture
def issuer(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, JWTVerifier]:
    monkeypatch.setenv(
        "EG_DEV_CLIENTS",
        '{"c1": {"secret": "s1", "tenant": "hospital-a", "scopes": ["check", "label"]},'
        ' "c2": {"secret": "s2", "tenant": "hospital-b", "scopes": ["check"]}}',
    )
    app = TestClient(create_app())
    verifier = JWTVerifier(
        "http://issuer/jwks", "evidencegate-dev", "evidencegate", MemoryTenants()
    )
    jwks = app.get("/.well-known/jwks.json").json()
    monkeypatch.setattr(verifier._jwks, "fetch_data", lambda: jwks)
    return app, verifier


def token(app: TestClient, client_id: str, secret: str, scope: str | None = None) -> Any:
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    data = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope
    return app.post("/oauth2/token", data=data, headers={"Authorization": f"Basic {basic}"})


def test_client_credentials_round_trip(issuer: tuple[TestClient, JWTVerifier]) -> None:
    app, verifier = issuer
    r = token(app, "c1", "s1")
    assert r.status_code == 200
    p = verifier.verify(f"Bearer {r.json()['access_token']}")
    assert p == Principal("c1", "hospital-a", frozenset({"check", "label"}))
    p.require("check")
    narrow = verifier.verify(f"Bearer {token(app, 'c1', 's1', 'check').json()['access_token']}")
    with pytest.raises(AuthError) as exc:
        narrow.require("label")
    assert exc.value.status == 403


def test_form_credentials_and_bad_requests(issuer: tuple[TestClient, JWTVerifier]) -> None:
    app, _ = issuer
    ok = app.post(
        "/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": "c2", "client_secret": "s2"},
    )
    assert ok.status_code == 200
    assert token(app, "c2", "wrong").status_code == 401
    assert token(app, "c2", "s2", "label").status_code == 400
    bad_grant = app.post("/oauth2/token", data={"grant_type": "password"})
    assert bad_grant.status_code == 400
    assert app.get("/healthz").json() == {"status": "ok"}


def test_rejects_missing_forged_and_expired_tokens(issuer: tuple[TestClient, JWTVerifier]) -> None:
    app, verifier = issuer
    with pytest.raises(AuthError):
        verifier.verify(None)
    good = token(app, "c1", "s1").json()["access_token"]
    header, payload, sig = good.split(".")
    with pytest.raises(AuthError):
        verifier.verify(f"Bearer {header}.{payload}.{sig[:-4]}AAAA")
    kid = jwt.get_unverified_header(good)["kid"]
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"iss": "evidencegate-dev", "aud": "evidencegate", "tenant": "x", "exp": time.time() + 60},
        other,
        algorithm="RS256",
        headers={"kid": kid},
    )
    with pytest.raises(AuthError):
        verifier.verify(f"Bearer {forged}")


def test_cognito_style_token_maps_client_to_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    pub.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    verifier = JWTVerifier(
        "http://x", "https://cognito", "unused", MemoryTenants({"app-123": "t1"})
    )
    monkeypatch.setattr(verifier._jwks, "fetch_data", lambda: {"keys": [pub]})
    # Cognito access tokens have client_id and scope but no aud.
    tok = jwt.encode(
        {
            "iss": "https://cognito",
            "client_id": "app-123",
            "scope": "evidencegate/check",
            "exp": time.time() + 60,
        },
        key,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    p = verifier.verify(f"Bearer {tok}")
    assert p.tenant == "t1"
    p.require("check")
    unknown = jwt.encode(
        {"iss": "https://cognito", "client_id": "nobody", "exp": time.time() + 60},
        key,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(AuthError) as exc:
        verifier.verify(f"Bearer {unknown}")
    assert exc.value.status == 403


def test_no_auth_is_explicit() -> None:
    p = NoAuth("t").verify(None)
    assert p.tenant == "t"
    assert "check" in p.scopes
