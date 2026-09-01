"""Клиент официального API hh.ru (https://api.hh.ru).

Работаем только через публичный API с OAuth-токеном пользователя — никакого парсинга
страниц и обхода защиты. hh.ru требует заголовок HH-User-Agent с названием приложения
и контактным email, иначе запросы отклоняются.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Iterator

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.hh.ru"


class HhApiError(RuntimeError):
    """Ошибка API hh.ru с разобранным телом ответа."""

    def __init__(self, status: int, payload: Any, url: str = "") -> None:
        self.status = status
        self.payload = payload
        self.url = url
        self.errors: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            self.errors = payload.get("errors") or []
        super().__init__(f"hh.ru {status} {url}: {payload}")

    @property
    def codes(self) -> set[str]:
        """Машиночитаемые коды ошибок, например {'already_applied'}."""
        out: set[str] = set()
        for err in self.errors:
            for key in ("value", "type"):
                if err.get(key):
                    out.add(str(err[key]))
        return out


class HhClient:
    def __init__(
        self,
        token_provider: Callable[[], str],
        user_agent: str,
        base_url: str = BASE_URL,
        http: httpx.Client | None = None,
        min_interval: float = 0.34,  # не чаще ~3 запросов в секунду
        max_retries: int = 4,
    ) -> None:
        self._token_provider = token_provider
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=30.0, follow_redirects=True)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request_at = 0.0

    # ---------- низкий уровень ----------

    def _headers(self, auth: bool = True) -> dict[str, str]:
        headers = {
            "HH-User-Agent": self.user_agent,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if auth:
            headers["Authorization"] = f"Bearer {self._token_provider()}"
        return headers

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last_request_at
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last_request_at = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        attempt = 0
        while True:
            self._throttle()
            resp = self._http.request(
                method, url, params=params, data=data, headers=self._headers(auth)
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise HhApiError(resp.status_code, self._payload(resp), url)
                delay = self._retry_delay(resp, attempt)
                log.warning("hh.ru %s — повтор через %.1fс (%s)", resp.status_code, delay, url)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise HhApiError(resp.status_code, self._payload(resp), url)
            if resp.status_code == 204 or not resp.content:
                return {}
            return self._payload(resp)

    @staticmethod
    def _payload(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return resp.text

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return min(60.0, (2**attempt) + random.uniform(0, 1))

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    # ---------- пользователь и резюме ----------

    def me(self) -> dict[str, Any]:
        return self.get("/me")

    def my_resumes(self) -> list[dict[str, Any]]:
        return self.get("/resumes/mine").get("items", [])

    # ---------- вакансии ----------

    def search_vacancies(self, params: dict[str, Any], max_pages: int = 3) -> Iterator[dict[str, Any]]:
        """Постраничный обход выдачи. hh.ru отдаёт максимум 2000 вакансий на запрос."""
        page = 0
        while page < max_pages:
            payload = self.get("/vacancies", params={**params, "page": page})
            items = payload.get("items", [])
            for item in items:
                yield item
            pages = payload.get("pages", 0)
            page += 1
            if page >= pages or not items:
                break

    def get_vacancy(self, vacancy_id: str) -> dict[str, Any]:
        return self.get(f"/vacancies/{vacancy_id}")

    # ---------- отклики ----------

    def apply(self, vacancy_id: str, resume_id: str, message: str | None = None) -> dict[str, Any]:
        """Отклик на вакансию (POST /negotiations, form-urlencoded)."""
        data: dict[str, Any] = {"vacancy_id": str(vacancy_id), "resume_id": str(resume_id)}
        if message:
            data["message"] = message
        return self.post("/negotiations", data=data)

    # ---------- переписка ----------

    def negotiations(self, per_page: int = 50, max_pages: int = 5, **filters: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 0
        while page < max_pages:
            payload = self.get(
                "/negotiations", params={"per_page": per_page, "page": page, **filters}
            )
            batch = payload.get("items", [])
            items.extend(batch)
            pages = payload.get("pages", 0)
            page += 1
            if page >= pages or not batch:
                break
        return items

    def negotiation(self, negotiation_id: str) -> dict[str, Any]:
        return self.get(f"/negotiations/{negotiation_id}")

    def messages(self, negotiation_id: str, per_page: int = 50) -> list[dict[str, Any]]:
        payload = self.get(
            f"/negotiations/{negotiation_id}/messages", params={"per_page": per_page}
        )
        return payload.get("items", [])

    def send_message(self, negotiation_id: str, text: str) -> dict[str, Any]:
        return self.post(
            f"/negotiations/{negotiation_id}/messages", data={"message": text}
        )

    # ---------- справочники ----------

    def dictionaries(self) -> dict[str, Any]:
        return self.get("/dictionaries", auth=False)

    def areas(self) -> list[dict[str, Any]]:
        return self.get("/areas", auth=False)

    def suggest_area(self, text: str) -> list[dict[str, Any]]:
        return self.get("/suggests/areas", params={"text": text}, auth=False).get("items", [])
