"""OAuth 2.0 для hh.ru: получение, хранение и обновление токенов.

Документация: https://api.hh.ru/openapi/redoc#tag/Avtorizaciya-prilozheniya
Приложение регистрируется на https://dev.hh.ru/admin
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
TOKEN_URL = "https://api.hh.ru/token"
LEGACY_TOKEN_URL = "https://hh.ru/oauth/token"  # запасной, если основной ответит 404


class AuthError(RuntimeError):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0  # unix-время истечения
    token_type: str = "bearer"

    @property
    def expired(self) -> bool:
        # обновляем заранее, за 5 минут до истечения
        return time.time() >= (self.expires_at - 300)

    @classmethod
    def from_response(cls, data: dict) -> "Tokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            expires_at=time.time() + float(data.get("expires_in", 0)),
            token_type=data.get("token_type", "bearer"),
        )


class TokenStore:
    """Хранит токены в JSON-файле с правами 0600."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> Tokens | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return Tokens(**data)

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(tokens), indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover — Windows
            pass


class HhAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        store: TokenStore,
        user_agent: str = "hhbot/0.1",
        http: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.user_agent = user_agent
        self._http = http or httpx.Client(timeout=30.0)
        self._tokens: Tokens | None = None

    # ---------- шаг 1: ссылка для входа ----------

    def authorize_url(self, state: str | None = None) -> tuple[str, str]:
        state = state or secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", state

    # ---------- шаг 2: обмен кода на токен ----------

    def _post_token(self, data: dict[str, str]) -> Tokens:
        headers = {
            "HH-User-Agent": self.user_agent,
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = self._http.post(TOKEN_URL, data=data, headers=headers)
        if resp.status_code == 404:  # старые приложения используют hh.ru/oauth/token
            resp = self._http.post(LEGACY_TOKEN_URL, data=data, headers=headers)
        if resp.status_code >= 400:
            raise AuthError(f"hh.ru вернул {resp.status_code}: {resp.text[:500]}")
        tokens = Tokens.from_response(resp.json())
        self.store.save(tokens)
        self._tokens = tokens
        return tokens

    def exchange_code(self, code: str) -> Tokens:
        return self._post_token(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "code": code,
            }
        )

    def refresh(self, tokens: Tokens) -> Tokens:
        if not tokens.refresh_token:
            raise AuthError("refresh_token отсутствует — нужна повторная авторизация (hhbot auth login)")
        # hh.ru принимает refresh только с grant_type и refresh_token
        return self._post_token(
            {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
        )

    # ---------- получение валидного токена ----------

    def access_token(self) -> str:
        tokens = self._tokens or self.store.load()
        if tokens is None:
            raise AuthError("Нет сохранённых токенов. Выполните: hhbot auth login")
        if tokens.expired:
            tokens = self.refresh(tokens)
        self._tokens = tokens
        return tokens.access_token

    # ---------- интерактивный вход ----------

    def login_interactive(self, open_browser: bool = True, timeout: int = 300) -> Tokens:
        """Поднимает локальный сервер на redirect_uri и ловит `code` после входа."""
        url, state = self.authorize_url()
        parsed = urllib.parse.urlparse(self.redirect_uri)
        if parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise AuthError(
                "Автоматический вход работает только с redirect_uri на localhost. "
                f"Откройте вручную:\n{url}\nи выполните: hhbot auth code <code>"
            )

        received: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                received.update({k: v[0] for k, v in qs.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                ok = "code" in received
                body = (
                    "<h2>Готово. Можно закрыть вкладку и вернуться в терминал.</h2>"
                    if ok
                    else f"<h2>Ошибка авторизации: {received}</h2>"
                )
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, *args: object) -> None:  # тишина в консоли
                return

        server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            print(f"Откройте ссылку и подтвердите доступ:\n{url}\n")
            if open_browser:
                webbrowser.open(url)
            deadline = time.time() + timeout
            while "code" not in received and time.time() < deadline:
                time.sleep(0.5)
        finally:
            server.shutdown()

        if "code" not in received:
            raise AuthError(f"Код авторизации не получен: {received or 'таймаут'}")
        if received.get("state") != state:
            raise AuthError("Несовпадение state — возможна подмена ответа, вход отменён")
        return self.exchange_code(received["code"])
