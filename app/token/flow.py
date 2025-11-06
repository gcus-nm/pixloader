from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, Dict
from urllib.parse import parse_qs, urlencode, urlparse

import requests

USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"


class PixivAuthError(RuntimeError):
    """Raised when the Pixiv OAuth flow fails."""


@dataclass
class OAuthSession:
    code_verifier: str
    login_url: str


@dataclass
class AuthTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": self.expires_in,
        }


def _s256(data: bytes) -> str:
    return urlsafe_b64encode(sha256(data).digest()).rstrip(b"=").decode("ascii")


def _oauth_pkce() -> tuple[str, str]:
    code_verifier = token_urlsafe(32)
    code_challenge = _s256(code_verifier.encode("ascii"))
    return code_verifier, code_challenge


def start_oauth_session() -> OAuthSession:
    code_verifier, code_challenge = _oauth_pkce()
    login_params = {
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    }
    login_url = f"{LOGIN_URL}?{urlencode(login_params)}"
    return OAuthSession(code_verifier=code_verifier, login_url=login_url)


def _request_token(payload: Dict[str, str]) -> Dict[str, Any]:
    try:
        response = requests.post(
            AUTH_TOKEN_URL,
            data=payload,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:  # pragma: no cover - network failure feedback
        raise PixivAuthError(f"Pixiv auth request failed: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise PixivAuthError("Pixiv auth response was not valid JSON") from exc

    if "refresh_token" not in data:
        raise PixivAuthError(f"Unexpected Pixiv auth response: {data}")

    return data


def _parse_tokens(data: Dict[str, Any]) -> AuthTokens:
    try:
        access_token = data["access_token"]
        refresh_token = data["refresh_token"]
    except KeyError as exc:
        raise PixivAuthError(f"Missing token field in response: {data}") from exc

    expires_raw = data.get("expires_in", 0)
    try:
        expires_in = int(expires_raw)
    except (TypeError, ValueError):
        expires_in = 0

    return AuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        raw=data,
    )


def exchange_code(session: OAuthSession, code: str) -> AuthTokens:
    data = _request_token(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "code_verifier": session.code_verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": REDIRECT_URI,
        }
    )
    return _parse_tokens(data)


def refresh_tokens(refresh_token: str) -> AuthTokens:
    data = _request_token(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "include_policy": "true",
            "refresh_token": refresh_token,
        }
    )
    return _parse_tokens(data)


def parse_code_from_input(raw_value: str) -> str:
    if not raw_value:
        return ""
    raw_value = raw_value.strip()
    if not raw_value:
        return ""
    if "code=" in raw_value:
        parsed = urlparse(raw_value)
        query = parsed.query or raw_value
        params = parse_qs(query)
        code_values = params.get("code")
        if code_values:
            return code_values[0]
    return raw_value

