"""Обёртка над Claude API: оценка вакансий, сопроводительные письма, ответы в чате."""

from __future__ import annotations

import logging
from datetime import datetime
from importlib import resources
from string import Template
from typing import Any, Literal, TypeVar

import anthropic
from pydantic import BaseModel, Field

from .config import LetterConfig, LLMConfig, ProfileConfig
from .models import Vacancy

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefusal(RuntimeError):
    """Модель отказалась отвечать (stop_reason='refusal')."""


# ---------- схемы структурированного вывода ----------


class VacancyScreen(BaseModel):
    fit_score: int = Field(ge=0, le=100)
    apply: bool
    reasons: list[str]
    matched_skills: list[str]
    gaps: list[str]
    hooks: list[str]
    red_flags: list[str]


class CoverLetter(BaseModel):
    body: str
    used_facts: list[str]
    self_check: str


class ProposedSlot(BaseModel):
    start_iso: str  # "" если время названо расплывчато
    end_iso: str
    raw: str


class MessageAnalysis(BaseModel):
    intent: Literal[
        "invitation", "question", "test_task", "rejection", "offer", "logistics", "spam", "other"
    ]
    summary: str
    proposed_slots: list[ProposedSlot]
    asks_availability: bool
    questions: list[str]
    answerable_from_profile: list[str]
    needs_human: bool
    escalation_reason: str
    interview_format: str
    location: str
    contact: str


class ReplyDraft(BaseModel):
    text: str
    self_check: str


# ---------- клиент ----------


def _prompt(name: str) -> Template:
    text = resources.files("hhbot.prompts").joinpath(name).read_text(encoding="utf-8")
    return Template(text)


def profile_to_text(profile: ProfileConfig) -> str:
    lines = [
        f"Имя: {profile.full_name or '—'}",
        f"Позиция: {profile.headline or '—'}",
        f"Опыт: {profile.years_experience} лет",
        f"О себе: {profile.summary or '—'}",
        f"Ключевые навыки: {', '.join(profile.key_skills) or '—'}",
        "Достижения: " + ("; ".join(profile.achievements) or "—"),
        f"Образование: {profile.education or '—'}",
        f"Языки: {', '.join(profile.languages) or '—'}",
        f"Город: {profile.city or '—'}; релокация: {'да' if profile.ready_to_relocate else 'нет'}; "
        f"только удалённо: {'да' if profile.remote_only else 'нет'}",
        "Зарплатные ожидания: "
        + (
            f"{profile.salary_expectation} {profile.salary_currency}"
            if profile.salary_expectation
            else "не раскрывать"
        ),
        f"Готовность выйти: {profile.notice_period or 'не указана'}",
    ]
    return "\n".join(lines)


class LLM:
    def __init__(
        self,
        config: LLMConfig,
        profile: ProfileConfig,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.client = client or anthropic.Anthropic()
        self._system = _prompt("system_common.md").substitute(profile=profile_to_text(profile))

    # ---------- низкий уровень ----------

    def _parse(self, user: str, schema: type[T], *, effort: str | None = None,
               max_tokens: int | None = None) -> T:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "system": [
                {"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": user}],
            "output_format": schema,
            "output_config": {"effort": effort or self.config.effort},
        }
        if self.config.thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if self.config.server_side_fallbacks:
            # страховка от stop_reason="refusal" у Opus 5: сервер сам подберёт запасную модель
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        response = self.client.beta.messages.parse(**kwargs)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMRefusal(f"Модель отклонила запрос: {details}")
        parsed = response.parsed_output
        if parsed is None:  # pragma: no cover — при корректной схеме не случается
            raise RuntimeError("Пустой структурированный ответ модели")
        return parsed

    # ---------- сценарии ----------

    def screen_vacancy(self, vacancy: Vacancy) -> VacancyScreen:
        user = _prompt("screen.md").substitute(
            vacancy=vacancy.to_prompt(),
            years=self.profile.years_experience,
            salary_expectation=self.profile.salary_expectation or "не указаны",
            currency=self.profile.salary_currency,
        )
        return self._parse(user, VacancyScreen, effort="medium")

    def write_cover_letter(
        self, vacancy: Vacancy, screen: VacancyScreen, letter: LetterConfig
    ) -> CoverLetter:
        salary_rule = (
            f"в конце одной фразой укажи зарплатные ожидания: {self.profile.salary_expectation} "
            f"{self.profile.salary_currency}"
            if letter.include_salary_expectation and self.profile.salary_expectation
            else "не упоминай зарплатные ожидания"
        )
        user = _prompt("cover_letter.md").substitute(
            vacancy=vacancy.to_prompt(max_description=4000),
            matched=", ".join(screen.matched_skills) or "—",
            hooks="; ".join(screen.hooks) or "—",
            gaps="; ".join(screen.gaps) or "—",
            language=letter.language,
            min_chars=letter.min_chars,
            max_chars=letter.max_chars,
            tone=letter.tone,
            salary_rule=salary_rule,
            must_mention="; ".join(letter.must_mention) or "—",
            forbid="; ".join(letter.forbid) or "—",
            signature_rule=(f"- подпись в конце: {letter.signature}" if letter.signature else ""),
            examples_rule=(
                "- ориентируйся на стиль этих образцов (не копируй текст):\n"
                + "\n---\n".join(letter.examples)
                if letter.examples
                else ""
            ),
        )
        return self._parse(user, CoverLetter)

    def analyze_message(
        self,
        *,
        vacancy_title: str,
        employer: str,
        state: str,
        history: str,
        new_messages: str,
        now: datetime,
        timezone_name: str,
    ) -> MessageAnalysis:
        user = _prompt("analyze_message.md").substitute(
            now=now.isoformat(timespec="minutes"),
            timezone=timezone_name,
            vacancy_title=vacancy_title,
            employer=employer,
            state=state or "—",
            history=history or "—",
            new_messages=new_messages,
        )
        return self._parse(user, MessageAnalysis)

    def compose_reply(
        self,
        *,
        vacancy_title: str,
        employer: str,
        new_messages: str,
        analysis: MessageAnalysis,
        slot_decision: str,
        now: datetime,
        timezone_name: str,
        language: str,
        max_chars: int,
        signature: str,
    ) -> ReplyDraft:
        user = _prompt("compose_reply.md").substitute(
            vacancy_title=vacancy_title,
            employer=employer,
            now=now.isoformat(timespec="minutes"),
            timezone=timezone_name,
            new_messages=new_messages,
            intent=analysis.intent,
            questions="; ".join(analysis.questions) or "—",
            answerable="; ".join(analysis.answerable_from_profile) or "—",
            slot_decision=slot_decision,
            language=language,
            max_chars=max_chars,
            signature=signature or self.profile.full_name or "—",
        )
        return self._parse(user, ReplyDraft)
