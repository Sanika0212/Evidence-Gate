"""OAuth2 client-credentials bearer tokens (JWT), verified against the issuer's JWKS.

In AWS the issuer is an Amazon Cognito user pool; locally it is the development issuer
in :mod:`evidencegate.devauth`. Either way the gateway only needs the JWKS URL, the
issuer and the audience. A token's tenant comes from its ``tenant`` claim when present,
otherwise from the tenant registry entry for its ``client_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, cast


class AuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Principal:
    client_id: str
    tenant: str
    scopes: frozenset[str] = field(default_factory=frozenset)

    def require(self, scope: str) -> None:
        # Cognito scopes look like "evidencegate/check"; accept the bare name too.
        if scope not in self.scopes and not any(s.endswith("/" + scope) for s in self.scopes):
            raise AuthError(403, f"token lacks the {scope!r} scope")


class TenantLookup(Protocol):
    def tenant_for(self, client_id: str) -> str | None: ...


class JWTVerifier:
    def __init__(self, jwks_url: str, issuer: str, audience: str, tenants: TenantLookup) -> None:
        import jwt

        self._jwt = jwt
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
        self.issuer = issuer
        self.audience = audience
        self.tenants = tenants

    def verify(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError(401, "missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            options: dict[str, Any] = {"require": ["exp", "iss"]}
            # Cognito access tokens carry client_id instead of aud.
            claims = self._jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options=cast(Any, {**options, "verify_aud": self._has_aud(token)}),
            )
        except self._jwt.PyJWTError as exc:
            raise AuthError(401, f"invalid token: {exc}") from exc
        client_id = str(claims.get("client_id") or claims.get("sub") or "")
        tenant = claims.get("tenant") or self.tenants.tenant_for(client_id)
        if not tenant:
            raise AuthError(403, "client is not registered with a tenant")
        scopes = frozenset(str(claims.get("scope", "")).split())
        return Principal(client_id=client_id, tenant=str(tenant), scopes=scopes)

    def _has_aud(self, token: str) -> bool:
        unverified = self._jwt.decode(token, options={"verify_signature": False})
        return "aud" in unverified


class NoAuth:
    """Local development only: every request belongs to one tenant with every scope."""

    def __init__(self, tenant: str = "demo") -> None:
        self.tenant = tenant

    def verify(self, authorization: str | None) -> Principal:
        return Principal("local", self.tenant, frozenset({"check", "label", "admin"}))
