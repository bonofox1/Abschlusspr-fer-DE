"""Minimal OAuth 2.1 authorization server for one owner.

Lets claude.ai (web, mobile, desktop) connect to a service as a custom connector.
claude.ai cannot send a fixed Bearer header, so it registers itself (RFC 7591),
sends the owner to a password page (authorization code + PKCE S256) and then
uses short-lived access tokens with refresh tokens.

All client IDs, codes and tokens are HMAC-signed and stateless, so they survive
restarts. Rotating OAUTH_SIGNING_KEY revokes everything at once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
DEFAULT_REDIRECT_PREFIXES = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 900


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(slots=True)
class OAuthServer:
    base_url: str
    signing_key: bytes
    login_password: str
    service_name: str
    redirect_prefixes: tuple[str, ...] = DEFAULT_REDIRECT_PREFIXES
    _used_codes: dict[str, float] = field(default_factory=dict)
    _failed: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls, service_name: str) -> "OAuthServer | None":
        base = os.getenv("MCP_PUBLIC_BASE_URL", "").strip().rstrip("/")
        key = os.getenv("OAUTH_SIGNING_KEY", "").strip()
        password = os.getenv("OAUTH_LOGIN_PASSWORD", "")
        if not (base and key and password):
            return None
        if not base.startswith("https://"):
            raise RuntimeError("MCP_PUBLIC_BASE_URL must use HTTPS when OAuth is enabled")
        if len(key) < 32 or len(password) < 12:
            raise RuntimeError("OAUTH_SIGNING_KEY needs 32+ and OAUTH_LOGIN_PASSWORD 12+ characters")
        extra = tuple(x.strip() for x in os.getenv("OAUTH_EXTRA_REDIRECT_PREFIXES", "").split(",") if x.strip())
        return cls(base, key.encode("utf-8"), password, service_name, DEFAULT_REDIRECT_PREFIXES + extra)

    # --- signed blobs -------------------------------------------------
    def _sign(self, payload: dict[str, object]) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        mac = _b64(hmac.new(self.signing_key, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{mac}"

    def _verify(self, token: str, typ: str) -> dict[str, object] | None:
        try:
            body, mac = token.split(".", 1)
            expected = _b64(hmac.new(self.signing_key, body.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(mac, expected):
                return None
            payload = json.loads(_unb64(body))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("typ") != typ:
            return None
        exp = payload.get("exp")
        if exp is not None and (not isinstance(exp, (int, float)) or exp < time.time()):
            return None
        return payload

    # --- metadata -----------------------------------------------------
    def resource_url(self) -> str:
        return f"{self.base_url}/mcp"

    def protected_resource_metadata(self) -> dict[str, object]:
        return {
            "resource": self.resource_url(),
            "authorization_servers": [self.base_url],
            "bearer_methods_supported": ["header"],
            "resource_name": self.service_name,
        }

    def authorization_server_metadata(self) -> dict[str, object]:
        return {
            "issuer": self.base_url,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "token_endpoint": f"{self.base_url}/token",
            "registration_endpoint": f"{self.base_url}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["read"],
        }

    def www_authenticate(self) -> str:
        return f'Bearer resource_metadata="{self.base_url}/.well-known/oauth-protected-resource"'

    # --- registration -------------------------------------------------
    def _redirect_allowed(self, uri: object) -> bool:
        if not isinstance(uri, str) or len(uri) > 512:
            return False
        parsed = urlparse(uri)
        if parsed.fragment or parsed.username or parsed.password:
            return False
        return any(uri == p or uri.startswith(p + "?") for p in self.redirect_prefixes)

    def register(self, body: object) -> dict[str, object]:
        if not isinstance(body, dict):
            raise OAuthError("invalid_client_metadata", "JSON object expected")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 5 or not all(self._redirect_allowed(u) for u in uris):
            raise OAuthError("invalid_redirect_uri", "Only Claude callback URLs are accepted")
        name = str(body.get("client_name") or "client")[:80]
        client_id = self._sign({"typ": "client", "r": uris, "n": name})
        return {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": name,
            "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

    def _client(self, client_id: object) -> dict[str, object]:
        payload = self._verify(client_id, "client") if isinstance(client_id, str) else None
        if payload is None:
            raise OAuthError("invalid_client", "Unknown client", 401)
        return payload

    # --- authorize ----------------------------------------------------
    def _check_authorize(self, q: dict[str, str]) -> dict[str, object]:
        client = self._client(q.get("client_id"))
        if q.get("redirect_uri") not in client["r"]:  # type: ignore[operator]
            raise OAuthError("invalid_request", "redirect_uri does not match the registered client")
        if q.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code is supported")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            raise OAuthError("invalid_request", "PKCE with S256 is required")
        resource = q.get("resource")
        if resource and resource.rstrip("/") not in (self.resource_url(), self.base_url):
            raise OAuthError("invalid_target", "Unknown resource")
        return client

    def login_page(self, query: str, message: str = "") -> str:
        q = {k: v[0] for k, v in parse_qs(query).items()}
        self._check_authorize(q)
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in q.items() if k in {"client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method", "state", "scope", "resource"}
        )
        note = f'<p style="color:#b00">{html.escape(message)}</p>' if message else ""
        return (
            "<!doctype html><html lang=de><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(self.service_name)} freigeben</title></head>"
            "<body style='font-family:system-ui;max-width:26rem;margin:3rem auto;padding:0 1rem'>"
            f"<h1 style='font-size:1.3rem'>{html.escape(self.service_name)}</h1>"
            "<p>Claude bittet um <b>Lesezugriff</b> auf diesen Dienst. Zum Freigeben das Dienst-Passwort eingeben.</p>"
            f"{note}<form method=post action='/authorize'>{hidden}"
            "<input type=password name=password autocomplete=current-password required autofocus "
            "style='width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box'>"
            "<button style='margin-top:1rem;padding:.6rem 1.2rem;font-size:1rem'>Freigeben</button></form></body></html>"
        )

    def authorize(self, form: dict[str, str]) -> str:
        """Returns the redirect URL on success; raises OAuthError otherwise."""
        self._check_authorize(form)
        now = time.time()
        with self._lock:
            self._failed[:] = [t for t in self._failed if now - t < LOCKOUT_SECONDS]
            if len(self._failed) >= MAX_FAILED_LOGINS:
                raise OAuthError("access_denied", "Zu viele Fehlversuche. Bitte 15 Minuten warten.", 429)
            if not hmac.compare_digest(form.get("password", "").encode("utf-8"), self.login_password.encode("utf-8")):
                self._failed.append(now)
                raise OAuthError("access_denied", "Passwort falsch.", 401)
            self._failed.clear()
        code = self._sign({
            "typ": "code", "exp": int(now) + CODE_TTL, "jti": secrets.token_urlsafe(12),
            "c": hashlib.sha256(form["client_id"].encode()).hexdigest(),
            "r": form["redirect_uri"], "pk": form["code_challenge"],
        })
        params = {"code": code}
        if form.get("state"):
            params["state"] = form["state"]
        params["iss"] = self.base_url
        sep = "&" if "?" in form["redirect_uri"] else "?"
        return form["redirect_uri"] + sep + urlencode(params)

    # --- token --------------------------------------------------------
    def _issue(self, client_id: str) -> dict[str, object]:
        now = int(time.time())
        cid = hashlib.sha256(client_id.encode()).hexdigest()
        return {
            "access_token": self._sign({"typ": "access", "exp": now + ACCESS_TTL, "aud": self.resource_url(), "c": cid}),
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": self._sign({"typ": "refresh", "exp": now + REFRESH_TTL, "c": cid, "jti": secrets.token_urlsafe(8)}),
            "scope": "read",
        }

    def token(self, form: dict[str, str]) -> dict[str, object]:
        grant = form.get("grant_type")
        client_id = form.get("client_id", "")
        self._client(client_id)
        cid = hashlib.sha256(client_id.encode()).hexdigest()
        if grant == "authorization_code":
            payload = self._verify(form.get("code", ""), "code")
            if payload is None or payload.get("c") != cid or payload.get("r") != form.get("redirect_uri"):
                raise OAuthError("invalid_grant", "Code invalid or expired")
            verifier = form.get("code_verifier", "")
            if not 43 <= len(verifier) <= 128 or _b64(hashlib.sha256(verifier.encode("ascii", "replace")).digest()) != payload.get("pk"):
                raise OAuthError("invalid_grant", "PKCE verification failed")
            jti = str(payload.get("jti"))
            now = time.time()
            with self._lock:
                for key, exp in list(self._used_codes.items()):
                    if exp < now:
                        del self._used_codes[key]
                if jti in self._used_codes:
                    raise OAuthError("invalid_grant", "Code already used")
                self._used_codes[jti] = float(payload["exp"])  # type: ignore[arg-type]
            return self._issue(client_id)
        if grant == "refresh_token":
            payload = self._verify(form.get("refresh_token", ""), "refresh")
            if payload is None or payload.get("c") != cid:
                raise OAuthError("invalid_grant", "Refresh token invalid or expired")
            return self._issue(client_id)
        raise OAuthError("unsupported_grant_type", "Unsupported grant_type")

    def valid_access_token(self, token: str) -> bool:
        payload = self._verify(token, "access")
        return payload is not None and payload.get("aud") == self.resource_url()
