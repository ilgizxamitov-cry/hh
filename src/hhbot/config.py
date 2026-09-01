"""Конфигурация бота: профиль соискателя, фильтры поиска, стиль писем, расписание."""

from __future__ import annotations

import os
from datetime import date, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path("config.yaml")


class AuthConfig(BaseModel):
    """OAuth-параметры приложения hh.ru. Секреты берутся из окружения."""

    client_id: str = Field(default_factory=lambda: os.getenv("HH_CLIENT_ID", ""))
    client_secret: str = Field(default_factory=lambda: os.getenv("HH_CLIENT_SECRET", ""))
    redirect_uri: str = Field(
        default_factory=lambda: os.getenv("HH_REDIRECT_URI", "http://localhost:8765/callback")
    )
    user_agent: str = Field(
        default_factory=lambda: os.getenv("HH_USER_AGENT", "hhbot/0.1 (unknown@example.com)")
    )
    token_file: Path = Path("state/tokens.json")


class ProfileConfig(BaseModel):
    """Факты о соискателе. Модель не имеет права выдумывать ничего сверх этого."""

    full_name: str = ""
    headline: str = ""  # «Backend-разработчик Python»
    years_experience: float = 0
    summary: str = ""
    key_skills: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    education: str = ""
    languages: list[str] = Field(default_factory=list)
    city: str = ""
    ready_to_relocate: bool = False
    remote_only: bool = False
    salary_expectation: int | None = None
    salary_currency: str = "RUR"
    contacts: dict[str, str] = Field(default_factory=dict)  # email/phone/telegram
    resume_id: str | None = None  # какое резюме отправлять; None → спросить/взять первое
    notice_period: str = ""  # «готов выйти через 2 недели»


class SearchQuery(BaseModel):
    """Один поисковый запрос к /vacancies. Поля повторяют параметры API hh.ru."""

    name: str = "default"
    text: str = ""
    area: list[int] = Field(default_factory=list)  # 1 = Москва, 2 = СПб, 113 = Россия
    professional_role: list[int] = Field(default_factory=list)
    experience: str | None = None  # noExperience|between1And3|between3And6|moreThan6
    employment: list[str] = Field(default_factory=list)  # full|part|project|probation
    schedule: list[str] = Field(default_factory=list)  # remote|fullDay|flexible|shift
    salary: int | None = None
    only_with_salary: bool = False
    period: int = 7  # глубина поиска в днях
    order_by: str = "publication_time"
    search_field: list[str] = Field(default_factory=list)  # name|company_name|description
    label: list[str] = Field(default_factory=list)  # not_from_agency|accept_handicapped|...
    per_page: int = 50
    max_pages: int = 3
    extra: dict[str, Any] = Field(default_factory=dict)  # любые прочие параметры API

    def to_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"per_page": min(self.per_page, 100), "period": self.period}
        if self.text:
            params["text"] = self.text
        if self.area:
            params["area"] = self.area
        if self.professional_role:
            params["professional_role"] = self.professional_role
        if self.experience:
            params["experience"] = self.experience
        if self.employment:
            params["employment"] = self.employment
        if self.schedule:
            params["schedule"] = self.schedule
        if self.salary is not None:
            params["salary"] = self.salary
        if self.only_with_salary:
            params["only_with_salary"] = "true"
        if self.order_by:
            params["order_by"] = self.order_by
        if self.search_field:
            params["search_field"] = self.search_field
        if self.label:
            params["label"] = self.label
        params.update(self.extra)
        return params


class FiltersConfig(BaseModel):
    """Жёсткие фильтры — применяются до обращения к модели, чтобы не жечь токены."""

    exclude_keywords: list[str] = Field(default_factory=list)  # в названии/описании
    exclude_employers: list[str] = Field(default_factory=list)
    require_keywords_any: list[str] = Field(default_factory=list)
    min_salary: int | None = None
    salary_currency: str = "RUR"
    treat_missing_salary_as_ok: bool = True
    skip_with_test: bool = True  # вакансии с тестом hh.ru нельзя откликнуть автоматически
    skip_agencies: bool = False
    max_age_days: int = 30
    allowed_experience: list[str] = Field(default_factory=list)
    min_fit_score: int = 65  # порог оценки модели (0-100) для отклика


class LetterConfig(BaseModel):
    """Правила для сопроводительного письма."""

    language: str = "ru"
    max_chars: int = 1400
    min_chars: int = 400
    tone: str = "деловой, тёплый, без канцелярита и лести"
    must_mention: list[str] = Field(default_factory=list)
    forbid: list[str] = Field(
        default_factory=lambda: [
            "шаблонные обороты вроде «я идеальный кандидат»",
            "перечисление всего резюме",
            "обещания, не подкреплённые фактами из профиля",
        ]
    )
    signature: str = ""
    include_salary_expectation: bool = False
    examples: list[str] = Field(default_factory=list)  # образцы вашего стиля (few-shot)


class TimeWindow(BaseModel):
    start: time
    end: time

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        if isinstance(v, str):
            h, m = v.split(":")[:2]
            return time(int(h), int(m))
        return v


class AvailabilityConfig(BaseModel):
    """Когда бот имеет право соглашаться на собеседование."""

    timezone: str = "Europe/Moscow"
    # 0 = понедельник ... 6 = воскресенье
    weekly: dict[int, list[TimeWindow]] = Field(
        default_factory=lambda: {
            0: [TimeWindow(start=time(11, 0), end=time(18, 0))],
            1: [TimeWindow(start=time(11, 0), end=time(18, 0))],
            2: [TimeWindow(start=time(11, 0), end=time(18, 0))],
            3: [TimeWindow(start=time(11, 0), end=time(18, 0))],
            4: [TimeWindow(start=time(11, 0), end=time(17, 0))],
        }
    )
    blackout_dates: list[date] = Field(default_factory=list)
    slot_minutes: int = 60
    buffer_minutes: int = 30  # зазор между встречами
    min_notice_hours: int = 24  # не соглашаться на встречу раньше, чем через N часов
    horizon_days: int = 14  # на сколько дней вперёд предлагать слоты
    max_per_day: int = 3
    formats: list[str] = Field(default_factory=lambda: ["online", "phone"])
    preferred_platforms: list[str] = Field(default_factory=lambda: ["Google Meet", "Zoom", "Telegram"])


class ChatConfig(BaseModel):
    """Поведение в переписке с работодателем."""

    autopilot: bool = False  # False → бот только готовит черновики, вы подтверждаете
    auto_confirm_interviews: bool = False  # автоматически подтверждать слот в окне доступности
    reply_language: str = "ru"
    max_reply_chars: int = 900
    poll_interval_minutes: int = 30
    # Любое из этих слов в сообщении работодателя → всегда эскалация человеку
    escalate_keywords: list[str] = Field(
        default_factory=lambda: [
            "оффер", "offer", "оклад", "зарплат", "договор", "трудовой",
            "паспорт", "снилс", "инн", "карта", "оплат", "перевод", "предоплат",
            "самозанят", "нда", "nda", "тестовое задание", "испытательный",
        ]
    )
    # Темы, на которые бот отвечать не будет никогда
    never_discuss: list[str] = Field(
        default_factory=lambda: [
            "финальные условия оффера и торг по зарплате",
            "персональные документы и платёжные данные",
            "юридические обязательства и подписание документов",
        ]
    )


class LimitsConfig(BaseModel):
    max_applications_per_day: int = 40
    max_applications_per_run: int = 15
    min_delay_seconds: float = 8.0
    max_delay_seconds: float = 25.0
    max_chat_replies_per_day: int = 30


class LLMConfig(BaseModel):
    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = 8000
    thinking: bool = True
    server_side_fallbacks: bool = True  # страховка на случай refusal у Opus 5


class BotConfig(BaseModel):
    auth: AuthConfig = Field(default_factory=AuthConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    searches: list[SearchQuery] = Field(default_factory=list)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    letter: LetterConfig = Field(default_factory=LetterConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    availability: AvailabilityConfig = Field(default_factory=AvailabilityConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    dry_run: bool = True  # по умолчанию ничего никуда не отправляется
    db_path: Path = Path("state/hhbot.db")

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "BotConfig":
        path = Path(path)
        data: dict[str, Any] = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth.token_file.parent.mkdir(parents=True, exist_ok=True)
