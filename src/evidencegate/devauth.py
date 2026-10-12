"""A minimal OAuth2 client-credentials issuer for local clusters (RS256 JWTs + JWKS).

In AWS this role belongs to Amazon Cognito; the gateway code is the same either way.
Clients come from ``EG_DEV_CLIENTS`` as JSON::

    {"demo-client": {"secret": "...", "tenant": "demo", "scopes": ["check", "label"]}}

The signing key is generated at start-up unless ``EG_DEV_SIGNING_KEY`` holds a PEM key,
so run one replica (or share the key).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from typing import Any

from fastapi import FastAPI, Form, Header, HTTPException


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def create_app(issuer: str = "evidencegate-dev", audience: str = "evidencegate") -> FastAPI:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = os.environ.get("EG_DEV_SIGNING_KEY")
    if pem:
        key: Any = serialization.load_pem_private_key(pem.encode(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = secrets.token_hex(8)
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64(numbers.n),
        "e": _b64(numbers.e),
    }
    default_clients = {
        "demo-client": {"secret": "demo-secret", "tenant": "demo", "scopes": ["check", "label"]}
    }
    raw_clients = os.environ.get("EG_DEV_CLIENTS")
    clients: dict[str, dict[str, Any]] = json.loads(raw_clients) if raw_clients else default_clients
    app = FastAPI(title="Evidence Gate dev issuer", docs_url=None, redoc_url=None)

    @app.get("/.well-known/jwks.json")
    def jwks() -> dict[str, Any]:
        return {"keys": [jwk]}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/oauth2/token")
    def token(
        grant_type: str = Form(...),
        client_id: str | None = Form(default=None),
        client_secret: str | None = Form(default=None),
        scope: str | None = Form(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if grant_type != "client_credentials":
            raise HTTPException(400, {"error": "unsupported_grant_type"})
        if authorization and authorization.lower().startswith("basic "):
            client_id, _, client_secret = (
                base64.b64decode(authorization[6:]).decode().partition(":")
            )
        client = clients.get(client_id or "")
        if client is None or not secrets.compare_digest(str(client["secret"]), client_secret or ""):
            raise HTTPException(401, {"error": "invalid_client"})
        allowed = set(client.get("scopes", []))
        requested = set((scope or "").split()) or allowed
        if not requested <= allowed:
            raise HTTPException(400, {"error": "invalid_scope"})
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": client_id,
            "client_id": client_id,
            "tenant": client["tenant"],
            "scope": " ".join(sorted(requested)),
            "iat": now,
            "exp": now + 3600,
        }
        signed = jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
        return {
            "access_token": signed,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": claims["scope"],
        }

    return app
