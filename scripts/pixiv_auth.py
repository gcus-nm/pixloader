#!/usr/bin/env python

from argparse import ArgumentParser
from webbrowser import open as open_url

from app.pixiv_auth_flow import (
    AuthTokens,
    PixivAuthError,
    exchange_code,
    parse_code_from_input,
    refresh_tokens,
    start_oauth_session,
)


def print_tokens(tokens: AuthTokens) -> None:
    print("access_token:", tokens.access_token)
    print("refresh_token:", tokens.refresh_token)
    print("expires_in:", tokens.expires_in)


def login(open_browser: bool = True) -> None:
    session = start_oauth_session()
    login_url = session.login_url

    if open_browser:
        try:
            opened = open_url(login_url)
        except Exception:
            opened = False
        if opened:
            print("Opened your default browser. Complete the Pixiv login and copy the resulting URL or code.")
        else:
            print("Failed to launch a browser automatically. Open this URL manually:")
            print(login_url)
    else:
        print("Open the following URL in your browser to authorize Pixiv access:")
        print(login_url)

    try:
        raw_value = input("callback URL or code: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    code = parse_code_from_input(raw_value)
    if not code:
        print("No authorization code provided.")
        return

    try:
        tokens = exchange_code(session, code)
    except PixivAuthError as exc:
        print(f"Failed to exchange authorization code: {exc}")
        return

    print_tokens(tokens)


def refresh(refresh_token: str) -> None:
    try:
        tokens = refresh_tokens(refresh_token)
    except PixivAuthError as exc:
        print(f"Failed to refresh Pixiv token: {exc}")
        return
    print_tokens(tokens)


# Web UI helper ------------------------------------------------------------

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
from urllib.parse import parse_qs, urlparse


def _new_flow_state():
    session = start_oauth_session()
    return {
        "session": session,
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

        code = parse_code_from_input(raw_value)
        if not code:
            self.server.state["error"] = "認証コード（またはコールバックURL）を貼り付けてください。"
            self._redirect("/")
            return

        try:
            tokens = exchange_code(self.server.state["session"], code)
        except PixivAuthError as exc:
            self.server.state["tokens"] = None
            self.server.state["error"] = f"トークン交換に失敗しました: {exc}"
        else:
            self.server.state["tokens"] = tokens
            self.server.state["error"] = None
            print_tokens(tokens)

        self._redirect("/")

    def _render_main_page(self):
        state = self.server.state
        session = state["session"]
        tokens = state.get("tokens")
        error = state.get("error")

        token_block = ""
        if isinstance(tokens, AuthTokens):
            token_block = f"""
            <section class=\"card success\">
              <h2>取得したトークン</h2>
              <p>以下の値を安全な場所にコピーしてください。</p>
              <label>Refresh Token</label>
              <textarea readonly rows=\"4\">{html.escape(tokens.refresh_token)}</textarea>
              <label>Access Token</label>
              <textarea readonly rows=\"4\">{html.escape(tokens.access_token)}</textarea>
              <p class=\"meta\">expires_in: {html.escape(str(tokens.expires_in))}</p>
              <p><a class=\"link\" href=\"/reset\">別アカウントでやり直す</a></p>
            </section>
            """

        error_block = ""
        if error:
            error_block = f"""
            <section class=\"alert error\">
              <strong>エラー:</strong> {html.escape(error)}
            </section>
            """

        page = f"""<!DOCTYPE html>
<html lang=\"ja\">
  <head>
    <meta charset=\"utf-8\">
    <title>Pixiv OAuth Helper</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: \"Segoe UI\", -apple-system, BlinkMacSystemFont, sans-serif;
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
      textarea {{
        width: 100%;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(10, 18, 32, 0.78);
        color: inherit;
        padding: 0.9rem 1rem;
        font-size: 1rem;
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
      label {{ font-weight: 600; }}
    </style>
  </head>
  <body>
    <main>
      <h1>Pixiv OAuth Helper</h1>
      <p>1. 下のボタンをクリックして Pixiv にログインします。</p>
      <p>2. ログイン後に表示される URL をコピーして戻り、このページのフォームに貼り付けてください。</p>

      <p><a class=\"button\" href=\"{html.escape(session.login_url)}\" target=\"_blank\" rel=\"noopener\">Pixivでログイン</a></p>

      <section class=\"card\">
        <form method=\"post\" action=\"/exchange\">
          <label for=\"code\">コールバックURLまたは認証コード</label>
          <textarea id=\"code\" name=\"code\" rows=\"3\" placeholder=\"https://app-api.pixiv.net/...code=...\"></textarea>
          <button class=\"submit\" type=\"submit\">トークン取得</button>
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
    print(f"OAuth helper running on http://{display_host}:{port}/")
    print("Open that URL in your browser, click the Pixiv login link, and paste the callback URL back into the page.")
    print(f"Direct login URL (for reference): {server.state['session'].login_url}")
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
