from __future__ import annotations

import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs
import json
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Pixloader Setup</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #0b1622;
        color: #e7efff;
        margin: 0;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      main {
        background: rgba(20, 36, 52, 0.95);
        padding: 2.2rem;
        border-radius: 16px;
        width: min(520px, 92vw);
        box-shadow: 0 18px 44px rgba(4, 13, 24, 0.62);
      }
      h1 {
        font-size: 1.8rem;
        margin-top: 0;
        margin-bottom: 0.6rem;
      }
      h2 {
        font-size: 1.1rem;
        margin-bottom: 0.8rem;
      }
      p {
        line-height: 1.6;
        margin-bottom: 1.2rem;
      }
      label {
        display: block;
        font-weight: 600;
        margin-bottom: 0.4rem;
      }
      input[type="password"],
      input[type="text"] {
        width: 100%;
        padding: 0.7rem 1rem;
        border-radius: 10px;
        border: 1px solid rgba(255, 255, 255, 0.12);
        background: rgba(3, 9, 17, 0.55);
        color: #e7efff;
        font-size: 1rem;
        outline: none;
        transition: border 0.2s ease;
      }
      input[type="password"]:focus,
      input[type="text"]:focus {
        border-color: rgba(53, 154, 255, 0.9);
      }
      button {
        margin-top: 1.2rem;
        width: 100%;
        padding: 0.8rem 1rem;
        border-radius: 10px;
        border: none;
        font-size: 1rem;
        font-weight: 600;
        background: linear-gradient(135deg, #3f8bff, #6d5dfc);
        color: white;
        cursor: pointer;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
      }
      button:hover {
        transform: translateY(-2px);
        box-shadow: 0 10px 24px rgba(63, 139, 255, 0.28);
      }
      .panel {
        background: rgba(11, 28, 46, 0.72);
        padding: 1.4rem 1.6rem;
        border-radius: 14px;
        margin-top: 1.6rem;
        border: 1px solid rgba(255, 255, 255, 0.08);
      }
      .panel-disabled {
        opacity: 0.6;
        border-style: dashed;
      }
      .note {
        font-size: 0.92rem;
        color: rgba(231, 239, 255, 0.72);
        margin-bottom: 1rem;
      }
      .success,
      .error,
      .info {
        margin-top: 1.1rem;
        padding: 0.9rem 1rem;
        border-radius: 10px;
        font-size: 0.95rem;
      }
      .success {
        background: rgba(60, 199, 129, 0.18);
        border: 1px solid rgba(60, 199, 129, 0.42);
        color: #9ff8c7;
      }
      .error {
        background: rgba(255, 102, 102, 0.12);
        border: 1px solid rgba(255, 132, 132, 0.4);
        color: #ffb7b7;
      }
      .info {
        background: rgba(130, 149, 255, 0.14);
        border: 1px solid rgba(130, 149, 255, 0.35);
        color: #bac8ff;
      }
      .hidden {
        display: none;
      }
      .intro {
        color: rgba(231, 239, 255, 0.75);
      }
      footer {
        margin-top: 2.2rem;
        font-size: 0.8rem;
        color: rgba(231, 239, 255, 0.54);
        line-height: 1.4;
      }
      code {
        background: rgba(231, 239, 255, 0.08);
        padding: 0.15rem 0.45rem;
        border-radius: 6px;
        font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        font-size: 0.85rem;
      }
    </style>
  </head>
  <body>
    <main>
      <h1>Pixloader Setup</h1>
      <p class="intro">
        Pixloader が Pixiv API にアクセスできるよう、リフレッシュトークンを登録してください。入力後はブラウザを閉じても構いません。
      </p>
      <section class="panel">
        <h2>既存のリフレッシュトークンを登録</h2>
        <form method="post" id="token-form" data-endpoint="/submit-token" data-success="#token-success" data-error="#token-error">
          <label for="token">Pixiv Refresh Token</label>
          <input id="token" type="password" name="token" required autocomplete="off" autofocus />
          <button type="submit">保存して開始する</button>
        </form>
        <div class="success hidden" id="token-success" data-default-text="トークンを保存しました。コンテナのログを確認してください。"></div>
        <div class="error hidden" id="token-error"></div>
      </section>
      <!--LOGIN_SECTION-->
      <footer>
        <p><strong>メモ:</strong> 入力された情報は Pixloader コンテナ内でのみ使用され、共有ボリュームにはリフレッシュトークンのみを保存します。共有環境ではアクセス権限など十分にご注意ください。</p>
      </footer>
    </main>
    <script>
      const forms = document.querySelectorAll("form[data-endpoint]");

      forms.forEach((form) => {
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          const endpoint = form.dataset.endpoint;
          const successEl = form.dataset.success ? document.querySelector(form.dataset.success) : null;
          const errorEl = form.dataset.error ? document.querySelector(form.dataset.error) : null;

          if (successEl) {
            successEl.classList.add("hidden");
            if (successEl.dataset.defaultText) {
              successEl.textContent = successEl.dataset.defaultText;
            } else {
              successEl.textContent = "";
            }
          }
          if (errorEl) {
            errorEl.classList.add("hidden");
            errorEl.textContent = "";
          }

          const formData = new FormData(form);
          const body = new URLSearchParams();
          for (const [key, value] of formData.entries()) {
            body.append(key, typeof value === "string" ? value : value.name || "");
          }

          let responseText = "";
          let responseStatus = 0;
          let responseData = null;
          try {
            const response = await fetch(endpoint, {
              method: "POST",
              headers: {
                "Content-Type": "application/x-www-form-urlencoded",
              },
              body,
            });
            responseStatus = response.status;
            const contentType = response.headers.get("content-type") || "";
            if (contentType.includes("application/json")) {
              responseData = await response.json();
              responseText = responseData.message || "";
            } else {
              responseText = await response.text();
            }
            if (response.ok) {
              if (successEl) {
                successEl.textContent = responseText || successEl.textContent;
                successEl.classList.remove("hidden");
              }
              form.reset();
              if (form.id === "login-form") {
                const otpField = document.querySelector("#otp-field");
                if (otpField) {
                  otpField.classList.add("hidden");
                }
              }
            } else {
              if (errorEl) {
                if (responseStatus === 428 && responseData && responseData.otp_required) {
                  const otpField = document.querySelector("#otp-field");
                  const otpInput = document.querySelector("#otp");
                  if (otpField) {
                    otpField.classList.remove("hidden");
                  }
                  if (otpInput) {
                    otpInput.focus();
                  }
                }
                errorEl.textContent = responseText || "エラーが発生しました。入力内容をご確認ください。";
                errorEl.classList.remove("hidden");
              } else {
                alert(responseText || "エラーが発生しました。");
              }
            }
          } catch (error) {
            if (errorEl) {
              errorEl.textContent = "ネットワークエラーが発生しました。しばらくしてから再試行してください。";
              errorEl.classList.remove("hidden");
            } else {
              alert("ネットワークエラーが発生しました。");
            }
          }
        });
      });
    </script>
  </body>
</html>
"""

@dataclass
class LoginResult:
    success: bool
    message: str
    refresh_token: str | None = None
    otp_required: bool = False


LOGIN_ENABLED_SECTION = """<section class="panel">
        <h2>Pixivアカウントでログインして取得</h2>
        <p class="note">
          入力された認証情報はリフレッシュトークンを取得する目的にのみ使用し、保存しません。なお、Pixiv 公式 API では 2022 年以降パスワードログインが廃止されているため、多くの環境ではこの方法は利用できません。公式の案内に従い、ブラウザ経由でリフレッシュトークンを取得することを推奨します。
        </p>
        <form method="post" id="login-form" data-endpoint="/submit-login" data-success="#login-success" data-error="#login-error">
          <label for="username">Pixiv ID / メールアドレス</label>
          <input id="username" type="text" name="username" required autocomplete="username" />
          <label for="password">パスワード</label>
          <input id="password" type="password" name="password" required autocomplete="current-password" />
          <div id="otp-field" class="hidden">
            <label for="otp">Pixiv 認証コード (6 桁)</label>
            <input id="otp" type="text" name="otp" inputmode="numeric" pattern="[0-9]*" maxlength="8" autocomplete="one-time-code" />
            <div class="info">Pixiv から届くメールに記載された認証コードを入力してください。</div>
          </div>
          <button type="submit">ログインしてトークンを取得</button>
        </form>
        <div class="success hidden" id="login-success" data-default-text="Pixiv にログインし、リフレッシュトークンを保存しました。コンテナのログを確認してください。"></div>
        <div class="error hidden" id="login-error"></div>
      </section>"""

LOGIN_DISABLED_SECTION = """<section class="panel panel-disabled">
        <h2>Pixivアカウントでログインして取得</h2>
        <p class="note">
          この機能は現在無効です。利用するには環境変数 <code>PIXLOADER_ALLOW_PASSWORD_LOGIN=true</code> を設定してコンテナを再起動してください。
        </p>
      </section>"""


def render_index_html(allow_password_login: bool) -> str:
    login_section = LOGIN_ENABLED_SECTION if allow_password_login else LOGIN_DISABLED_SECTION
    return HTML_TEMPLATE.replace("<!--LOGIN_SECTION-->", login_section)


class TokenHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that stores tokens to disk."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        handler_class,
        token_file: Path,
        event: threading.Event,
        allow_password_login: bool,
        login_handler: Optional[Callable[[str, str, Optional[str]], LoginResult]],
    ) -> None:
        super().__init__(server_address, handler_class)
        self.token_file = token_file
        self.token_event = event
        self.allow_password_login = allow_password_login
        self._login_handler = login_handler
        self._file_lock = threading.Lock()

    def store_token(self, token: str) -> None:
        with self._file_lock:
            self.token_file.write_text(token, encoding="utf-8")
        self.token_event.set()

    def login_and_get_token(self, username: str, password: str, otp: Optional[str]) -> LoginResult:
        if not self.allow_password_login or self._login_handler is None:
            return LoginResult(success=False, message="Password login is disabled.")
        return self._login_handler(username, password, otp)


class TokenRequestHandler(BaseHTTPRequestHandler):
    server: TokenHTTPServer  # type: ignore[assignment]

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond_text("ok")
            return
        self._respond_html(render_index_html(self.server.allow_password_login))

    def do_POST(self) -> None:
        if self.path == "/submit-token":
            self._handle_submit_token()
        elif self.path == "/submit-login":
            self._handle_submit_login()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _handle_submit_token(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        params = parse_qs(raw_body)
        token = params.get("token", [""])[0].strip()

        if not token:
            self._respond_text("Token is required.", status=HTTPStatus.BAD_REQUEST)
            return

        LOGGER.info("Received refresh token via web form (length %s).", len(token))
        self.server.store_token(token)
        self._respond_text("Stored refresh token. You may close this window.")

    def _handle_submit_login(self) -> None:
        if not self.server.allow_password_login:
            self._respond_text("Password login is disabled.", status=HTTPStatus.FORBIDDEN)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        params = parse_qs(raw_body)

        username = params.get("username", [""])[0].strip()
        password = params.get("password", [""])[0]
        otp = params.get("otp", [""])[0].strip() if "otp" in params else None

        if not username or not password:
            self._respond_text("Username and password are required.", status=HTTPStatus.BAD_REQUEST)
            return

        LOGGER.info("Received Pixiv credential login attempt for user '%s'.", _mask_username(username))
        result = self.server.login_and_get_token(username, password, otp)

        # Reduce credential exposure in memory
        password = ""

        if result.success and result.refresh_token:
            self.server.store_token(result.refresh_token)
            self._respond_json({"success": True, "message": "Pixiv にログインし、リフレッシュトークンを保存しました。"})
        elif result.otp_required:
            self._respond_json(
                {
                    "success": False,
                    "message": result.message or "Pixiv の認証コードを入力してください。",
                    "otp_required": True,
                },
                status=HTTPStatus.PRECONDITION_REQUIRED,
            )
        else:
            self._respond_json(
                {
                    "success": False,
                    "message": result.message or "ログインに失敗しました。",
                    "otp_required": False,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

    def log_message(self, format: str, *args) -> None:  # noqa: A003 - signature defined by BaseHTTPRequestHandler
        LOGGER.debug("TokenServer: " + format, *args)

    def _respond_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _respond_text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
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


class TokenInputServer:
    """Starts a lightweight HTTP server to collect tokens from a browser."""

    def __init__(self, token_file: Path, port: int, allow_password_login: bool) -> None:
        self._token_file = token_file
        self._port = port
        self._allow_password_login = allow_password_login
        self._event = threading.Event()
        self._server: Optional[TokenHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def obtain_token(self, stop_event: threading.Event) -> str | None:
        try:
            self._server = TokenHTTPServer(
                ("0.0.0.0", self._port),
                TokenRequestHandler,
                self._token_file,
                self._event,
                self._allow_password_login,
                self._login_with_pixiv if self._allow_password_login else None,
            )
        except OSError as exc:
            LOGGER.error("Failed to start token server on port %s: %s", self._port, exc)
            return None

        self._thread = threading.Thread(target=self._server.serve_forever, name="TokenServer", daemon=True)
        self._thread.start()
        LOGGER.info(
            "Waiting for Pixiv refresh token. Open http://localhost:%s/ in your browser to submit it.",
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

    def _login_with_pixiv(self, username: str, password: str, otp: Optional[str]) -> LoginResult:
        LOGGER.warning(
            "Password-based Pixiv login attempted for user '%s', but this flow is deprecated by the official API.",
            _mask_username(username),
        )
        return LoginResult(success=False, message=PASSWORD_LOGIN_DEPRECATED_MESSAGE, otp_required=False)


def _mask_username(username: str) -> str:
    if not username:
        return "<unknown>"
    if len(username) <= 2:
        return username[0] + "*"
    return username[:2] + "***" + username[-1]


def _is_otp_required(body: str) -> bool:
    text = body.lower()
    keywords = [
        "verification code",
        "check your email",
        "authentication code",
        "two-factor",
        "otp",
        "確認コード",
        "認証コード",
    ]
    return any(keyword in text for keyword in keywords)


PASSWORD_LOGIN_DEPRECATED_MESSAGE = (
    "Pixiv 公式 API ではパスワードログインが廃止されているため、この方法ではリフレッシュトークンを取得できません。"
    "ブラウザ経由の OAuth フロー（例: get-pixivpy-token など）で取得したトークンを入力してください。\n"
    "詳細: https://github.com/upbit/pixivpy/issues/158"
)
