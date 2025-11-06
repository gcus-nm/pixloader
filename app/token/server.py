from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .flow import (
    AuthTokens,
    OAuthSession,
    PixivAuthError,
    exchange_code,
    parse_code_from_input,
    start_oauth_session,
)

LOGGER = logging.getLogger(__name__)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <title>Pixloader Pixiv Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      :root {
        color-scheme: dark;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "Segoe UI", "Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif;
        background: linear-gradient(180deg, #070c17 0%, #0e172a 100%);
        min-height: 100vh;
        color: #f4f7ff;
      }
      main {
        max-width: 760px;
        margin: 0 auto;
        padding: clamp(2rem, 6vw, 3.4rem) clamp(1.2rem, 4vw, 2.4rem) 4rem;
      }
      h1 {
        margin-top: 0;
        font-size: clamp(2rem, 6vw, 2.5rem);
        font-weight: 700;
      }
      p.lead {
        color: rgba(230, 238, 255, 0.85);
        line-height: 1.8;
        margin-bottom: 1.2rem;
      }
      a.button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0.85rem 1.8rem;
        border-radius: 999px;
        text-decoration: none;
        font-weight: 600;
        background: linear-gradient(135deg, #4c78ff, #8a58ff);
        color: #fff;
        box-shadow: 0 18px 36px rgba(38, 62, 140, 0.35);
        transition: transform 0.12s ease, box-shadow 0.12s ease;
      }
      a.button:hover {
        transform: translateY(-2px);
        box-shadow: 0 22px 46px rgba(42, 72, 155, 0.45);
      }
      section.card {
        margin-top: 2.4rem;
        padding: clamp(1.6rem, 4vw, 2.2rem);
        border-radius: 22px;
        background: rgba(12, 22, 38, 0.78);
        border: 1px solid rgba(255, 255, 255, 0.12);
        box-shadow: 0 18px 36px rgba(4, 10, 24, 0.45);
      }
      section.card.success {
        border-color: rgba(124, 212, 173, 0.55);
        background: rgba(18, 46, 34, 0.72);
      }
      label {
        font-weight: 600;
        margin-bottom: 0.4rem;
        display: block;
      }
      textarea {
        width: 100%;
        padding: 0.9rem 1rem;
        border-radius: 14px;
        border: 1px solid rgba(255, 255, 255, 0.16);
        background: rgba(5, 12, 28, 0.7);
        color: inherit;
        font-size: 1rem;
        resize: vertical;
      }
      textarea[readonly] {
        cursor: text;
        background: rgba(9, 20, 38, 0.82);
      }
      .actions {
        margin-top: 0.9rem;
        display: flex;
        gap: 0.8rem;
        flex-wrap: wrap;
      }
      button {
        padding: 0.65rem 1.4rem;
        border-radius: 999px;
        border: none;
        font-weight: 600;
        cursor: pointer;
        background: rgba(255, 255, 255, 0.14);
        color: inherit;
        transition: transform 0.12s ease, box-shadow 0.12s ease;
      }
      button.primary {
        background: linear-gradient(135deg, #4c78ff, #8a58ff);
        color: #fff;
        box-shadow: 0 14px 28px rgba(42, 68, 146, 0.35);
      }
      button.ghost {
        background: rgba(255, 255, 255, 0.08);
      }
      button:hover {
        transform: translateY(-1px);
      }
      .alert {
        margin-top: 1.4rem;
        padding: 1rem 1.2rem;
        border-radius: 14px;
        border: 1px solid rgba(255, 144, 144, 0.45);
        background: rgba(255, 92, 92, 0.16);
        color: #ffcaca;
      }
      .meta {
        font-size: 0.9rem;
        opacity: 0.8;
        margin-top: 0.5rem;
      }
      pre.code-snippet {
        overflow-x: auto;
        padding: 0.9rem 1rem;
        border-radius: 12px;
        border: 1px solid rgba(255, 255, 255, 0.12);
        background: rgba(0, 0, 0, 0.35);
      }
    </style>
  </head>
  <body>
    <main>
      <h1>Pixiv ログイン</h1>
      <p class="lead">Pixiv アカウントでログインし、取得されたリフレッシュトークンを Pixloader に登録します。</p>

      <a id="loginLink" class="button" href="#" target="_blank" rel="noopener">Pixivでログイン</a>

      <section class="card" id="inputCard">
        <label for="codeInput">コールバックURLまたは認証コード</label>
        <textarea id="codeInput" rows="3" placeholder="https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?..."></textarea>
        <div class="actions">
          <button type="button" class="primary" id="submitButton">トークン取得</button>
          <button type="button" id="pasteButton">クリップボードから貼り付け</button>
          <button type="button" class="ghost" id="resetButton">新しいログインセッション</button>
        </div>
        <p class="meta">※ 認証後に表示されたページの URL を貼り付けると自動的にコードが抽出されます。</p>
      </section>

      <div id="errorBox" class="alert" hidden></div>

      <section class="card success" id="tokensCard" hidden>
        <h2>取得したトークン</h2>
        <p>以下の値が安全に保存されました。必要に応じてアクセス・リフレッシュトークンをコピーしてください。</p>
        <label>Refresh Token</label>
        <textarea id="refreshOutput" rows="3" readonly></textarea>
        <label>Access Token</label>
        <textarea id="accessOutput" rows="3" readonly></textarea>
        <p class="meta">expires_in: <span id="expiresOutput"></span> 秒</p>
      </section>
    </main>

    <script>
    (function() {
      const loginLink = document.getElementById('loginLink');
      const codeInput = document.getElementById('codeInput');
      const submitButton = document.getElementById('submitButton');
      const pasteButton = document.getElementById('pasteButton');
      const resetButton = document.getElementById('resetButton');
      const errorBox = document.getElementById('errorBox');
      const tokensCard = document.getElementById('tokensCard');
      const refreshOutput = document.getElementById('refreshOutput');
      const accessOutput = document.getElementById('accessOutput');
      const expiresOutput = document.getElementById('expiresOutput');

      async function fetchState() {
        const response = await fetch('/state', { cache: 'no-store' });
        if (!response.ok) {
          throw new Error('state fetch failed');
        }
        return response.json();
      }

      async function refreshState() {
        try {
          const state = await fetchState();
          loginLink.href = state.session.login_url;
          if (state.tokens) {
            tokensCard.hidden = false;
            refreshOutput.value = state.tokens.refresh_token;
            accessOutput.value = state.tokens.access_token;
            expiresOutput.textContent = state.tokens.expires_in;
          }
          if (state.error) {
            errorBox.hidden = false;
            errorBox.textContent = state.error;
          } else {
            errorBox.hidden = true;
            errorBox.textContent = '';
          }
        } catch (err) {
          console.error(err);
        }
      }

      submitButton.addEventListener('click', async () => {
        const payload = { code: codeInput.value };
        const response = await fetch('/exchange', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (response.ok) {
          codeInput.value = '';
        }
        await refreshState();
      });

      pasteButton.addEventListener('click', async () => {
        try {
          const text = await navigator.clipboard.readText();
          codeInput.value = text;
        } catch (err) {
          console.error(err);
        }
      });

      resetButton.addEventListener('click', async () => {
        await fetch('/reset', { method: 'POST' });
        await refreshState();
      });

      refreshState();
    }());
    </script>
  </body>
</html>
"""


@dataclass
class TokenServerState:
    session: OAuthSession
    tokens: AuthTokens | None = None
    error: str | None = None

    def to_payload(self) -> dict:
        return {
            "session": {
                "login_url": self.session.login_url,
            },
            "tokens": self.tokens.to_dict() if self.tokens else None,
            "error": self.error,
        }


class TokenHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], token_file: Path, event: threading.Event) -> None:
        self.state = TokenServerState(session=start_oauth_session())
        self.token_file = token_file
        self.token_event = event
        super().__init__(address, TokenRequestHandler)

    def reset_flow(self) -> None:
        self.state = TokenServerState(session=start_oauth_session())

    def record_tokens(self, tokens: AuthTokens) -> None:
        self.state.tokens = tokens
        self.state.error = None
        self.store_token(tokens.refresh_token)

    def store_token(self, token: str) -> None:
        self.token_file.write_text(token, encoding="utf-8")
        self.token_event.set()


class TokenRequestHandler(BaseHTTPRequestHandler):
    server: TokenHTTPServer  # type: ignore[assignment]

    def do_GET(self) -> None:
        if self.path in {"/", ""}:
            self._respond_html(HTML_TEMPLATE)
            return
        if self.path == "/state":
            payload = self.server.state.to_payload()
            self._respond_json(payload)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        if self.path == "/exchange":
            self._handle_exchange()
            return
        if self.path == "/reset":
            self.server.reset_flow()
            self._respond_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _handle_exchange(self) -> None:
        try:
            payload = self._json_body()
        except ValueError:
            self._respond_json({"error": "無効なリクエストです。"}, status=HTTPStatus.BAD_REQUEST)
            return

        raw_value = str(payload.get("code", "")).strip()
        code = parse_code_from_input(raw_value)
        if not code:
            self.server.state.error = "認証コード（またはコールバックURL）を貼り付けてください。"
            self._respond_json({"error": self.server.state.error}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            tokens = exchange_code(self.server.state.session, code)
        except PixivAuthError as exc:
            LOGGER.warning("Pixiv auth failed: %s", exc)
            self.server.state.tokens = None
            self.server.state.error = str(exc)
            self._respond_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self.server.record_tokens(tokens)
        response = self.server.state.to_payload()
        response["ok"] = True
        self._respond_json(response)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def _respond_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _respond_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # type: ignore[override]
        LOGGER.debug("TokenServer: " + format, *args)


class TokenInputServer:
    """Starts a lightweight HTTP server to collect tokens from a browser."""

    def __init__(self, token_file: Path, port: int, allow_password_login: bool) -> None:  # noqa: ARG002
        self._token_file = token_file
        self._port = port
        self._event = threading.Event()
        self._server: Optional[TokenHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def obtain_token(self, stop_event: threading.Event) -> str | None:
        try:
            self._server = TokenHTTPServer(("0.0.0.0", self._port), self._token_file, self._event)
        except OSError as exc:
            LOGGER.error("Failed to start token server on port %s: %s", self._port, exc)
            return None

        self._thread = threading.Thread(target=self._server.serve_forever, name="TokenServer", daemon=True)
        self._thread.start()
        LOGGER.info(
            "Waiting for Pixiv refresh token. Open http://localhost:%s/ in your browser to complete setup.",
            self._port,
        )

        try:
            while not stop_event.is_set():
                if self._event.wait(timeout=1):
                    break
        finally:
            if self._server:
                self._server.shutdown()
            if self._thread:
                self._thread.join(timeout=5)

        if stop_event.is_set() and not self._event.is_set():
            LOGGER.info("Shutdown requested before refresh token was provided.")
            return None

        if self._token_file.exists():
            token = self._token_file.read_text(encoding="utf-8").strip()
            return token or None

        return None

