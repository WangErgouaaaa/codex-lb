from __future__ import annotations

import base64
import hashlib
import urllib.parse

import pytest

from app.core.clients.oauth import build_authorization_url, pkce_challenge
from app.core.config.settings import get_settings

pytestmark = pytest.mark.unit


def test_pkce_challenge_matches_sha256():
    verifier = "test_verifier"
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert pkce_challenge(verifier) == expected


def test_build_authorization_url_contains_required_params():
    url = build_authorization_url(
        state="state_123",
        code_challenge="challenge_456",
        base_url="https://auth.openai.com",
        client_id="client_id",
        redirect_uri="http://localhost:1455/auth/callback",
        scope="openid profile email offline_access",
    )

    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"

    query = urllib.parse.parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client_id"]
    assert query["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert query["scope"] == ["openid profile email offline_access"]
    assert query["code_challenge"] == ["challenge_456"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state_123"]
    assert query["id_token_add_organizations"] == ["true"]
    assert query["codex_cli_simplified_flow"] == ["true"]
    assert query["originator"] == ["codex_chatgpt_desktop"]


def test_build_authorization_url_ignores_removed_originator_env(monkeypatch: pytest.MonkeyPatch):
    # CODEX_LB_OAUTH_ORIGINATOR was removed from the settings surface
    # (issue #1340); the originator is a fixed protocol constant now.
    monkeypatch.setenv("CODEX_LB_OAUTH_ORIGINATOR", "codex_cli_rs")
    get_settings.cache_clear()

    try:
        url = build_authorization_url(
            state="state_123",
            code_challenge="challenge_456",
            base_url="https://auth.openai.com",
            client_id="client_id",
            redirect_uri="http://localhost:1455/auth/callback",
            scope="openid profile email offline_access",
        )
    finally:
        get_settings.cache_clear()

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert query["originator"] == ["codex_chatgpt_desktop"]


def test_build_authorization_url_allows_explicit_originator_override():
    url = build_authorization_url(
        state="state_123",
        code_challenge="challenge_456",
        base_url="https://auth.openai.com",
        client_id="client_id",
        originator="codex_cli_rs",
        redirect_uri="http://localhost:1455/auth/callback",
        scope="openid profile email offline_access",
    )

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert query["originator"] == ["codex_cli_rs"]


@pytest.mark.asyncio
async def test_device_code_request_omits_user_agent_header(monkeypatch):
    """The CLI's device-flow client is a bare reqwest build without a
    User-Agent; the pre-auth egress must not carry aiohttp's Python UA."""
    from app.core.clients import oauth as oauth_module
    from app.core.clients.oauth import OAuthError, request_device_code

    captured: dict[str, object] = {}

    class _Response:
        status = 400

        async def json(self, content_type=None):
            return {"error": {"message": "temporarily_unavailable"}}

        async def text(self):
            return "temporarily_unavailable"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Session:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return _Response()

    class _Lease:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(oauth_module, "lease_http_session", lambda session=None: _Lease())

    with pytest.raises(OAuthError):
        await request_device_code(allow_direct_egress=True)

    assert captured["url"].endswith("/api/accounts/deviceauth/usercode")
    assert captured["skip_auto_headers"] == ("User-Agent",)


@pytest.mark.asyncio
async def test_authorization_code_exchange_omits_user_agent_header(monkeypatch):
    from app.core.clients import oauth as oauth_module
    from app.core.clients.oauth import OAuthError, exchange_authorization_code

    captured: dict[str, object] = {}

    class _Response:
        status = 400

        async def json(self, content_type=None):
            return {"error": {"message": "invalid_grant"}}

        async def text(self):
            return "invalid_grant"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Session:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return _Response()

    class _Lease:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(oauth_module, "lease_http_session", lambda session=None: _Lease())

    with pytest.raises(OAuthError):
        await exchange_authorization_code(
            code="code",
            code_verifier="verifier",
            allow_direct_egress=True,
        )

    assert captured["url"].endswith("/oauth/token")
    assert captured["skip_auto_headers"] == ("User-Agent",)
