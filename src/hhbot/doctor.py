"""Проверка подключения: ключи, конфиг, токен, доступ к API hh.ru."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

from .api import HhApiError
from .auth import TokenStore
from .config import BotConfig

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PLACEHOLDER_EMAILS = ("unknown@example.com", "your@email.com", "you@example.com")


@dataclass
class Check:
    name: str
    status: str  # ok | fail | warn
    detail: str = ""
    hint: str = ""


def _ok(name: str, detail: str = "") -> Check:
    return Check(name, "ok", detail)


def _fail(name: str, detail: str, hint: str = "") -> Check:
    return Check(name, "fail", detail, hint)


def _warn(name: str, detail: str, hint: str = "") -> Check:
    return Check(name, "warn", detail, hint)


# ---------- ключи и окружение ----------


def check_credentials(config: BotConfig) -> list[Check]:
    auth = config.auth
    checks: list[Check] = []

    for label, value, hint in (
        ("HH_CLIENT_ID", auth.client_id, "создайте приложение на https://dev.hh.ru/admin"),
        ("HH_CLIENT_SECRET", auth.client_secret, "секрет приложения оттуда же"),
    ):
        checks.append(_ok(label, "задан") if value else _fail(label, "не задан", hint))

    if not auth.redirect_uri:
        checks.append(_fail("HH_REDIRECT_URI", "не задан", "например http://localhost:8765/callback"))
    else:
        localhost = any(host in auth.redirect_uri for host in ("localhost", "127.0.0.1"))
        checks.append(
            _ok("HH_REDIRECT_URI", auth.redirect_uri)
            if localhost
            else _warn(
                "HH_REDIRECT_URI",
                auth.redirect_uri,
                "не localhost: автоматический перехват кода не сработает, "
                "используйте `hhbot auth code <url>`",
            )
        )

    if not EMAIL_RE.search(auth.user_agent or ""):
        checks.append(
            _fail(
                "HH_USER_AGENT",
                auth.user_agent or "пусто",
                "hh.ru требует формат «MyApp/1.0 (my@email.com)» — без email запросы отклоняются",
            )
        )
    elif any(stub in auth.user_agent for stub in PLACEHOLDER_EMAILS):
        checks.append(
            _fail(
                "HH_USER_AGENT",
                auth.user_agent,
                "это email из шаблона — подставьте свой настоящий",
            )
        )
    else:
        checks.append(_ok("HH_USER_AGENT", auth.user_agent))

    key = os.getenv("ANTHROPIC_API_KEY", "")
    checks.append(
        _ok("ANTHROPIC_API_KEY", "задан")
        if key
        else _fail("ANTHROPIC_API_KEY", "не задан", "ключ с https://console.anthropic.com")
    )
    return checks


# ---------- конфигурация ----------


def check_config(config: BotConfig) -> list[Check]:
    checks: list[Check] = []
    profile = config.profile

    if not config.searches:
        checks.append(_fail("searches", "нет ни одного поискового запроса", "заполните config.yaml"))
    else:
        names = ", ".join(query.name for query in config.searches)
        checks.append(_ok("searches", f"{len(config.searches)}: {names}"))

    missing = [
        label
        for label, value in (
            ("full_name", profile.full_name),
            ("headline", profile.headline),
            ("summary", profile.summary),
            ("key_skills", profile.key_skills),
        )
        if not value
    ]
    if missing:
        checks.append(
            _fail(
                "profile",
                "не заполнено: " + ", ".join(missing),
                "это единственный источник фактов для писем — без него письма будут общими",
            )
        )
    else:
        checks.append(_ok("profile", f"{profile.headline}, навыков: {len(profile.key_skills)}"))

    if not profile.achievements:
        checks.append(_warn("profile.achievements", "пусто", "добавьте 2-3 факта с цифрами"))

    checks.append(
        _ok("profile.resume_id", profile.resume_id)
        if profile.resume_id
        else _warn("profile.resume_id", "не задан", "будет взято первое резюме; см. `hhbot resumes`")
    )

    checks.append(
        _warn("dry_run", "включён", "реальная отправка — `hhbot --live ...` или dry_run: false")
        if config.dry_run
        else _ok("dry_run", "выключен — отправка реальная")
    )
    return checks


# ---------- токен ----------


def check_token(config: BotConfig) -> Check:
    store = TokenStore(config.auth.token_file)
    try:
        tokens = store.load()
    except (ValueError, TypeError, OSError) as exc:
        return _fail("токен hh.ru", f"файл повреждён: {exc}", "выполните `hhbot auth login`")

    if tokens is None:
        return _fail("токен hh.ru", "не найден", "выполните `hhbot auth login`")
    if tokens.expired and not tokens.refresh_token:
        return _fail("токен hh.ru", "истёк, refresh_token отсутствует", "`hhbot auth login`")
    if tokens.expired:
        return _warn("токен hh.ru", "истекает, будет обновлён автоматически")

    hours_left = (tokens.expires_at - time.time()) / 3600
    return _ok("токен hh.ru", f"действителен ещё ~{hours_left:.0f} ч")


# ---------- доступ к API ----------


def check_api(client: Any, config: BotConfig) -> list[Check]:
    checks: list[Check] = []

    try:
        client.dictionaries()
        checks.append(_ok("api.hh.ru доступен", "GET /dictionaries"))
    except Exception as exc:  # сеть, прокси, DNS
        return [
            _fail(
                "api.hh.ru доступен",
                str(exc)[:200],
                "проверьте сеть и прокси: без доступа к api.hh.ru бот работать не может",
            )
        ]

    offline_hint = (
        "поиск через API закрыт — работайте офлайн: `hhbot letter -f вакансия.txt` "
        "и `hhbot reply -f переписка.txt`"
    )
    try:
        next(iter(client.search_vacancies({"text": "python", "per_page": 1}, max_pages=1)), None)
        checks.append(_ok("поиск вакансий", "GET /vacancies отвечает"))
    except HhApiError as exc:
        if exc.status in (401, 403):
            checks.append(
                _warn("поиск вакансий", f"hh.ru {exc.status}: доступ закрыт", offline_hint)
            )
        else:
            checks.append(_fail("поиск вакансий", f"hh.ru {exc.status}: {str(exc.payload)[:80]}"))
    except Exception as exc:
        checks.append(_warn("поиск вакансий", str(exc)[:160], offline_hint))

    auth_hint = (
        "выполните `hhbot auth login`; если соискательский API недоступен "
        "(закрыт hh.ru 15.12.2025) — работайте офлайн: `hhbot letter` и `hhbot reply`"
    )
    try:
        me = client.me()
    except HhApiError as exc:
        codes = ", ".join(sorted(exc.codes)) or str(exc.payload)[:120]
        return checks + [_warn("соискательский API", f"hh.ru {exc.status}: {codes}", auth_hint)]
    except Exception as exc:
        return checks + [_warn("соискательский API", str(exc)[:200], auth_hint)]

    name = f"{me.get('first_name', '')} {me.get('last_name', '')}".strip() or me.get("email", "")
    checks.append(_ok("соискательский API", f"{name} ({me.get('email', '—')})"))
    if me.get("is_applicant") is False:
        checks.append(
            _fail("тип аккаунта", "это аккаунт работодателя", "войдите под аккаунтом соискателя")
        )

    try:
        resumes = client.my_resumes()
    except Exception as exc:
        checks.append(_fail("резюме", str(exc)[:200]))
        return checks

    if not resumes:
        checks.append(_fail("резюме", "на аккаунте нет резюме", "создайте резюме на hh.ru"))
        return checks

    titles = ", ".join(f"{r.get('title')} ({r.get('id')})" for r in resumes[:3])
    checks.append(_ok("резюме", f"{len(resumes)}: {titles}"))

    resume_id = config.profile.resume_id
    if resume_id and resume_id not in {str(r.get("id")) for r in resumes}:
        checks.append(
            _fail(
                "profile.resume_id",
                f"{resume_id} нет среди ваших резюме",
                "возьмите id из `hhbot resumes`",
            )
        )
    return checks


def run_checks(config: BotConfig, client: Any | None = None) -> list[Check]:
    checks = check_credentials(config) + check_config(config) + [check_token(config)]
    if client is None:
        checks.append(
            _warn("api.hh.ru", "проверка пропущена", "не удалось создать клиент — см. ошибки выше")
        )
    else:
        checks.extend(check_api(client, config))
    return checks
