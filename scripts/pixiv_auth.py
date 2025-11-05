#!/usr/bin/env python

from argparse import ArgumentParser
from base64 import urlsafe_b64encode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pprint import pprint
from secrets import token_urlsafe
from sys import exit
from urllib.parse import urlencode, urlparse, parse_qs
from webbrowser import open as open_url

import html
import requests

# Latest app version can be found using GET /v1/application-info/android
USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"


def s256(data):
    """S256 transformation method."""

    return urlsafe_b64encode(sha256(data).digest()).rstrip(b"=").decode("ascii")


def oauth_pkce(transform):
    """Proof Key for Code Exchange by OAuth Public Clients (RFC7636)."""

    code_verifier = token_urlsafe(32)
    code_challenge = transform(code_verifier.encode("ascii"))

    return code_verifier, code_challenge


def build_authorization_request():
    code_verifier, code_challenge = oauth_pkce(s256)
    login_params = {
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    }
    login_url = f"{LOGIN_URL}?{urlencode(login_params)}"
    return code_verifier, login_url


def extract_tokens(data):
    try:
        access_token = data["access_token"]
        refresh_token = data["refresh_token"]
    except KeyError as exc:
        raise ValueError(f"Unexpected response: {data}") from exc
    expires_in = data.get("expires_in", 0)
    return access_token, refresh_token, expires_in


def print_auth_token_response(data):
    try:
        access_token, refresh_token, expires_in = extract_tokens(data)
    except ValueError:
        print("error:")
        pprint(data)
        exit(1)

    print("access_token:", access_token)
    print("refresh_token:", refresh_token)
    print("expires_in:", expires_in)


def request_auth_token(payload):
    try:
        response = requests.post(
            AUTH_TOKEN_URL,
            data=payload,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Pixiv auth request failed: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Could not decode Pixiv response: {response.text}") from exc

    extract_tokens(data)  # Validate payload contains the expected fields.
    return data


def exchange_authorization_code(code, code_verifier):
    return request_auth_token(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": REDIRECT_URI,
        }
    )


def refresh_with_token(refresh_token):
    return request_auth_token(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "include_policy": "true",
            "refresh_token": refresh_token,
        }
    )


def login(open_browser: bool = True) -> None:
    code_verifier, login_url = build_authorization_request()

    if open_browser:
        try:
            opened = open_url(login_url)
        except Exception:
            opened = False
        if opened:
            print("Opened your default browser. Complete the Pixiv login and copy the resulting code parameter.")
        else:
            print("Failed to launch a browser automatically. Open this URL manually:")
            print(login_url)
    else:
        print("Open the following URL in your browser to authorize Pixiv access:")
        print(login_url)

    try:
        code = input("code: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    try:
        data = exchange_authorization_code(code, code_verifier)
    except RuntimeError as exc:
        print(f"Failed to exchange authorization code: {exc}")
        return

    print_auth_token_response(data)


def refresh(refresh_token):
    try:
        data = refresh_with_token(refresh_token)
    except RuntimeError as exc:
        print(f"Failed to refresh token: {exc}")
        return
    print_auth_token_response(data)


def _new_flow_state():
    code_verifier, login_url = build_authorization_request()
    return {
        "code_verifier": code_verifier,
        "login_url": login_url,
        "tokens": None,
        "error": None,
    }


class OAuthWebRequestHandler(BaseHTTPRequestHandler):
    server_version = "PixivAuthHelper/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/reset":
            self.server.reset_flow()
            self._redirect("/")
            return
        if parsed.path in {"/", ""}:
            self._render_main_page()
            return
        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/exchange":
            self.send_error(404, "Not Found")
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8")
        fields = parse_qs(raw_body)
        raw_value = fields.get("code", [""])[0].strip()

        code = self._extract_code(raw_value)
        if not code:
            self.server.state["error"] = "認証コード（またはコールバックURL）を貼り付けてください。"
            self._redirect("/")
            return

        try:
            data = exchange_authorization_code(code, self.server.state["code_verifier"])
        except RuntimeError as exc:
            self.server.state["tokens"] = None
            self.server.state["error"] = f"トークン交換に失敗しました: {exc}"
        else:
            self.server.state["tokens"] = data
            self.server.state["error"] = None
            print_auth_token_response(data)

        self._redirect("/")

    def _extract_code(self, raw_value: str) -> str:
        if not raw_value:
            return ""
        if "code=" in raw_value:
            parsed = urlparse(raw_value)
            query = parsed.query or raw_value
            params = parse_qs(query)
            if "code" in params:
                return params["code"][0]
        return raw_value

    def _render_main_page(self):
        state = self.server.state
        login_url = state["login_url"]
        tokens = state.get("tokens")
        error = state.get("error")

        token_block = ""
        if tokens:
            access_token, refresh_token, expires_in = extract_tokens(tokens)
            token_block = f"""
            <section class="card success">
              <h2>取得したトークン</h2>
              <p>以下の値を安全な場所にコピーしてください。</p>
              <label>Refresh Token</label>
              <textarea readonly rows="4">{html.escape(refresh_token)}</textarea>
              <label>Access Token</label>
              <textarea readonly rows="4">{html.escape(access_token)}</textarea>
              <p class="meta">expires_in: {html.escape(str(expires_in))}</p>
              <p><a class="link" href="/reset">別アカウントでやり直す</a></p>
            </section>
            """

        error_block = ""
        if error:
            error_block = f"""
            <section class="alert error">
              <strong>エラー:</strong> {html.escape(error)}
            </section>
            """

        page = f"""<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <title>Pixiv OAuth Helper</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
        background: linear-gradient(180deg, #090f1a 0%, #0e1727 100%);
        color: #f1f5ff;
        min-height: 100vh;
      }}
      main {{
        max-width: 680px;
        margin: 0 auto;
        padding: 3rem 1.4rem 4rem;
      }}
      h1 {{
        margin-top: 0;
        font-size: 1.9rem;
        font-weight: 700;
      }}
      p {{ line-height: 1.6; }}
      a.button {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0.75rem 1.6rem;
        border-radius: 999px;
        background: linear-gradient(135deg, #4c78ff, #7d56ff);
        color: #fff;
        text-decoration: none;
        font-weight: 600;
        margin-top: 0.6rem;
      }}
      form {{
        margin-top: 1.8rem;
        display: grid;
        gap: 0.6rem;
      }}
      textarea, input[type="text"] {{
        width: 100%;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(10, 18, 32, 0.78);
        color: inherit;
        padding: 0.9rem 1rem;
        font-size: 1rem;
        resize: vertical;
      }}
      button.submit {{
        justify-self: flex-start;
        padding: 0.7rem 1.6rem;
        border-radius: 999px;
        border: none;
        background: rgba(255,255,255,0.12);
        color: inherit;
        font-weight: 600;
        cursor: pointer;
      }}
      .card {{
        margin-top: 2.2rem;
        padding: 1.8rem 1.6rem;
        border-radius: 18px;
        background: rgba(12, 24, 42, 0.82);
        border: 1px solid rgba(255,255,255,0.12);
      }}
      .card.success {{
        border-color: rgba(108, 212, 164, 0.4);
        background: rgba(24, 58, 44, 0.6);
      }}
      .alert {{
        margin-top: 1.2rem;
        padding: 1rem 1.2rem;
        border-radius: 12px;
        border: 1px solid rgba(255, 148, 148, 0.42);
        background: rgba(255, 102, 102, 0.18);
        color: #ffc7c7;
      }}
      .meta {{
        margin-top: 0.8rem;
        font-size: 0.9rem;
        opacity: 0.8;
      }}
      label {{ font-weight: 600; }}
      textarea[readonly] {{ cursor: text; }}
      .link {{ color: #9fb7ff; }}
    </style>
  </head>
  <body>
    <main>
      <h1>Pixiv OAuth Helper</h1>
      <p>1. 下のボタンをクリックして Pixiv にログインします（別タブで開きます）。</p>
      <p>2. ログイン後に表示される URL から <code>code=...</code> を含むコールバック URL をコピーしてください。</p>
      <p>3. コピーした内容をフォームに貼り付けて「トークン取得」ボタンを押すと、リフレッシュトークンなどが表示されます。</p>

      <p><a class="button" href="{html.escape(login_url)}" target="_blank" rel="noopener">Pixivでログイン</a></p>

      <section class="card">
        <form method="post" action="/exchange">
          <label for="code">コールバックURLまたは認証コード</label>
          <textarea id="code" name="code" rows="3" placeholder="https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=...&code=..."></textarea>
          <button class="submit" type="submit">トークン取得</button>
        </form>
      </section>

      {error_block}
      {token_block}
    </main>
  </body>
</html>
"""
        self._write_html(page)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _write_html(self, content: str):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # type: ignore[override]
        return


class OAuthWebServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address):
        super().__init__(server_address, OAuthWebRequestHandler)
        self.state = _new_flow_state()

    def reset_flow(self):
        self.state = _new_flow_state()


def serve_web(host: str, port: int) -> None:
    display_host = "localhost" if host in {"0.0.0.0", "::"} else host
    server = OAuthWebServer((host, port))
    login_url = server.state["login_url"]
    print(f"OAuth helper running on http://{display_host}:{port}/")
    print("Open that URL in your browser, click the Pixiv login link, and paste the callback URL back into the page.")
    print(f"Direct login URL (for reference): {login_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping helper...")
    finally:
        server.server_close()


def main():
    parser = ArgumentParser()
    subparsers = parser.add_subparsers()
    parser.set_defaults(func=lambda _: parser.print_usage())
    login_parser = subparsers.add_parser("login")
    login_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of trying to launch a browser.",
    )
    login_parser.set_defaults(func=lambda ns: login(open_browser=not ns.no_browser))
    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("refresh_token")
    refresh_parser.set_defaults(func=lambda ns: refresh(ns.refresh_token))

    serve_parser = subparsers.add_parser("serve", help="Start a local web UI to guide the OAuth flow.")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0).")
    serve_parser.add_argument("--port", type=int, default=8150, help="Port to listen on (default: 8150).")
    serve_parser.set_defaults(func=lambda ns: serve_web(host=ns.host, port=ns.port))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
