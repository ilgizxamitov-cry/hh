"""Нормализация объектов hh.ru в удобный вид для фильтров и промптов."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str:
    """Описание вакансии приходит в HTML — превращаем в читаемый текст."""
    if not raw:
        return ""
    text = re.sub(r"<(br|/p|/li|/div)[^>]*>", "\n", raw, flags=re.I)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return _NL_RE.sub("\n\n", text).strip()


@dataclass
class Vacancy:
    id: str
    name: str
    employer: str
    employer_id: str = ""
    area: str = ""
    url: str = ""
    description: str = ""
    key_skills: list[str] = field(default_factory=list)
    experience: str = ""
    employment: str = ""
    schedule: str = ""
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str = ""
    salary_gross: bool | None = None
    published_at: str = ""
    archived: bool = False
    has_test: bool = False
    response_letter_required: bool = False
    already_responded: bool = False
    is_agency: bool = False
    professional_roles: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Vacancy":
        salary = data.get("salary") or data.get("salary_range") or {}
        employer = data.get("employer") or {}
        relations = data.get("relations") or []
        return cls(
            id=str(data.get("id", "")),
            name=data.get("name") or "",
            employer=employer.get("name") or "",
            employer_id=str(employer.get("id") or ""),
            area=(data.get("area") or {}).get("name") or "",
            url=data.get("alternate_url") or "",
            description=html_to_text(data.get("description")),
            key_skills=[s.get("name", "") for s in (data.get("key_skills") or [])],
            experience=(data.get("experience") or {}).get("id") or "",
            employment=(data.get("employment") or {}).get("id") or "",
            schedule=(data.get("schedule") or {}).get("id") or "",
            salary_from=salary.get("from"),
            salary_to=salary.get("to"),
            salary_currency=salary.get("currency") or "",
            salary_gross=salary.get("gross"),
            published_at=data.get("published_at") or "",
            archived=bool(data.get("archived")),
            has_test=bool(data.get("has_test")),
            response_letter_required=bool(data.get("response_letter_required")),
            already_responded=any("got_response" in str(r) or "responded" in str(r) for r in relations),
            # Точный признак даёт только параметр поиска label=not_from_agency;
            # здесь — грубая эвристика по названию компании как подстраховка.
            is_agency=any(
                word in (employer.get("name") or "").lower()
                for word in ("кадров", "рекрут", "агентств", "staffing", "recruit")
            ),
            professional_roles=[r.get("name", "") for r in (data.get("professional_roles") or [])],
            raw=data,
        )

    @property
    def age_days(self) -> float | None:
        if not self.published_at:
            return None
        try:
            published = datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - published).total_seconds() / 86400

    @property
    def salary_text(self) -> str:
        if self.salary_from is None and self.salary_to is None:
            return "не указана"
        parts = []
        if self.salary_from:
            parts.append(f"от {self.salary_from:,}".replace(",", " "))
        if self.salary_to:
            parts.append(f"до {self.salary_to:,}".replace(",", " "))
        gross = "" if self.salary_gross is None else (" до вычета" if self.salary_gross else " на руки")
        return f"{' '.join(parts)} {self.salary_currency}{gross}".strip()

    def to_prompt(self, max_description: int = 6000) -> str:
        """Компактное текстовое представление для модели."""
        desc = self.description[:max_description]
        return "\n".join(
            [
                f"Вакансия: {self.name}",
                f"Компания: {self.employer}",
                f"Город: {self.area}",
                f"Зарплата: {self.salary_text}",
                f"Опыт: {self.experience or 'не указан'}; график: {self.schedule or 'не указан'}; "
                f"занятость: {self.employment or 'не указана'}",
                f"Ключевые навыки: {', '.join(self.key_skills) or '—'}",
                f"Ссылка: {self.url}",
                "",
                "Описание:",
                desc,
            ]
        )


@dataclass
class Message:
    id: str
    author: str  # "employer" | "applicant"
    text: str
    created_at: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Message":
        return cls(
            id=str(data.get("id", "")),
            author=(data.get("author") or {}).get("participant_type") or "",
            text=html_to_text(data.get("text")),
            created_at=data.get("created_at") or "",
        )

    @property
    def from_employer(self) -> bool:
        return self.author == "employer"


@dataclass
class Negotiation:
    id: str
    state: str
    vacancy_id: str
    vacancy_name: str
    employer: str
    has_updates: bool = False
    unread: int = 0
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Negotiation":
        vacancy = data.get("vacancy") or {}
        counters = data.get("counters") or {}
        return cls(
            id=str(data.get("id", "")),
            state=(data.get("state") or {}).get("id") or "",
            vacancy_id=str(vacancy.get("id") or ""),
            vacancy_name=vacancy.get("name") or "",
            employer=(vacancy.get("employer") or {}).get("name") or "",
            has_updates=bool(data.get("has_updates")),
            unread=int(counters.get("unread_messages") or 0),
            url=vacancy.get("alternate_url") or "",
            raw=data,
        )
