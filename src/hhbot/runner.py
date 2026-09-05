"""Оркестрация: цикл откликов, цикл переписки и объединённый прогон."""

from __future__ import annotations

import logging
import random
import time
from datetime import date
from dataclasses import dataclass, field

from .api import HhApiError, HhClient
from .chat import ChatAgent, ChatOutcome
from .config import BotConfig
from .letters import build_cover_letter
from .llm import LLM
from .matching import hard_filter, prescore
from .models import Vacancy
from .storage import Storage

log = logging.getLogger(__name__)

# Ошибки hh.ru, после которых продолжать прогон бессмысленно
FATAL_APPLY_CODES = {"limit_exceeded", "daily_limit_exceeded", "negotiations_limit_exceeded"}
# Насколько ниже порога может быть грубая оценка, чтобы вакансия всё же дошла до модели
PRESCORE_MARGIN = 25


@dataclass
class ApplyOutcome:
    vacancy_id: str
    title: str
    employer: str
    url: str
    status: str  # applied | dry_run | skipped | failed
    reason: str = ""
    fit_score: int | None = None
    letter: str = ""
    screen_notes: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    applications: list[ApplyOutcome] = field(default_factory=list)
    chats: list[ChatOutcome] = field(default_factory=list)

    @property
    def applied(self) -> list[ApplyOutcome]:
        return [a for a in self.applications if a.status in {"applied", "dry_run", "prepared"}]


class Runner:
    def __init__(self, config: BotConfig, client: HhClient, llm: LLM, storage: Storage) -> None:
        self.config = config
        self.client = client
        self.llm = llm
        self.storage = storage

    # ---------- резюме ----------

    def resolve_resume_id(self) -> str:
        if self.config.profile.resume_id:
            return self.config.profile.resume_id
        resumes = self.client.my_resumes()
        if not resumes:
            raise RuntimeError("На аккаунте hh.ru нет ни одного резюме")
        resume_id = str(resumes[0]["id"])
        log.warning(
            "resume_id не задан в конфиге, использую первое резюме: %s (%s)",
            resumes[0].get("title"), resume_id,
        )
        return resume_id

    # ---------- отклики ----------

    def _sleep_between_applications(self) -> None:
        delay = random.uniform(
            self.config.limits.min_delay_seconds, self.config.limits.max_delay_seconds
        )
        log.debug("пауза %.1f с", delay)
        time.sleep(delay)

    def _evaluate(self, vacancy: Vacancy, outcome: ApplyOutcome) -> bool:
        """Фильтры → оценка → письмо. False, если вакансия отсеяна (причина уже в outcome)."""
        check = hard_filter(vacancy, self.config.filters, self.config.profile)
        if not check.passed:
            outcome.reason = check.reason
            self.storage.remember_vacancy(vacancy.raw, "skipped", check.reason)
            return False

        rough = prescore(vacancy, self.config.profile)
        if rough < self.config.filters.min_fit_score - PRESCORE_MARGIN:
            outcome.reason = f"грубая оценка {rough} слишком низкая, модель не вызывалась"
            outcome.fit_score = rough
            self.storage.remember_vacancy(vacancy.raw, "skipped", outcome.reason, rough)
            return False

        screen = self.llm.screen_vacancy(vacancy)
        outcome.fit_score = screen.fit_score
        if not screen.apply or screen.fit_score < self.config.filters.min_fit_score:
            reason = f"оценка {screen.fit_score}: " + "; ".join(screen.reasons[:3])
            outcome.reason = reason
            self.storage.remember_vacancy(vacancy.raw, "skipped", reason, screen.fit_score)
            return False

        letter, letter_check = build_cover_letter(self.llm, vacancy, screen, self.config.letter)
        outcome.letter = letter
        outcome.screen_notes = screen.reasons[:3]
        if not letter_check.ok:
            log.warning("письмо для %s с замечаниями: %s", vacancy.id, letter_check.problems)
        return True

    def prepare_once(self, vacancy: Vacancy) -> ApplyOutcome:
        """Готовит письмо, но не отправляет: отклик вы делаете руками на сайте."""
        outcome = ApplyOutcome(
            vacancy_id=vacancy.id,
            title=vacancy.name,
            employer=vacancy.employer,
            url=vacancy.url,
            status="skipped",
        )
        if not self._evaluate(vacancy, outcome):
            return outcome

        outcome.status = "prepared"
        outcome.reason = "письмо готово, отклик отправьте сами"
        self.storage.remember_vacancy(vacancy.raw, "prepared", outcome.reason, outcome.fit_score)
        self.storage.record_application(vacancy.id, None, outcome.letter, "prepared")
        return outcome

    def apply_once(self, vacancy: Vacancy, resume_id: str) -> ApplyOutcome:
        outcome = ApplyOutcome(
            vacancy_id=vacancy.id,
            title=vacancy.name,
            employer=vacancy.employer,
            url=vacancy.url,
            status="skipped",
        )
        if not self._evaluate(vacancy, outcome):
            return outcome

        screen_score = outcome.fit_score
        letter = outcome.letter

        if self.config.dry_run:
            outcome.status = "dry_run"
            outcome.reason = "dry_run: отклик не отправлен"
            self.storage.remember_vacancy(vacancy.raw, "applied", "dry_run", screen_score)
            self.storage.record_application(vacancy.id, resume_id, letter, "dry_run")
            return outcome

        try:
            self.client.apply(vacancy.id, resume_id, letter)
        except HhApiError as exc:
            codes = exc.codes
            outcome.status = "failed"
            outcome.reason = f"hh.ru {exc.status}: {', '.join(sorted(codes)) or exc.payload}"
            self.storage.remember_vacancy(vacancy.raw, "error", outcome.reason, screen_score)
            self.storage.record_application(
                vacancy.id, resume_id, letter, "failed", error=outcome.reason
            )
            if codes & FATAL_APPLY_CODES:
                raise
            return outcome

        outcome.status = "applied"
        self.storage.remember_vacancy(vacancy.raw, "applied", "отклик отправлен", screen_score)
        self.storage.record_application(vacancy.id, resume_id, letter, "sent")
        self.storage.bump("applications")
        return outcome

    def run_applications(
        self, limit: int | None = None, mode: str = "apply"
    ) -> list[ApplyOutcome]:
        """mode="apply" — отклик через API; mode="prepare" — только письма, без отправки.

        Режим prepare не требует токена соискателя: поиск и карточка вакансии на hh.ru
        доступны анонимно.
        """
        resume_id = "" if mode == "prepare" else self.resolve_resume_id()
        limits = self.config.limits
        budget = limit if limit is not None else limits.max_applications_per_run
        if mode == "prepare":
            remaining_today = budget
        else:
            remaining_today = limits.max_applications_per_day - self.storage.count("applications")
            if remaining_today <= 0:
                log.warning("дневной лимит откликов исчерпан (%s)", limits.max_applications_per_day)
                return []
        budget = min(budget, remaining_today)

        outcomes: list[ApplyOutcome] = []
        sent = 0

        for query in self.config.searches:
            if sent >= budget:
                break
            log.info("Поиск «%s»: %s", query.name, query.text)
            for item in self.client.search_vacancies(query.to_params(), query.max_pages):
                if sent >= budget:
                    break
                vacancy_id = str(item.get("id"))
                if self.storage.is_known(vacancy_id) or self.storage.has_applied(vacancy_id):
                    continue
                try:
                    full = self.client.get_vacancy(vacancy_id)
                except HhApiError as exc:
                    log.warning("не удалось получить вакансию %s: %s", vacancy_id, exc)
                    continue

                vacancy = Vacancy.from_api(full)
                try:
                    outcome = (
                        self.prepare_once(vacancy)
                        if mode == "prepare"
                        else self.apply_once(vacancy, resume_id)
                    )
                except HhApiError as exc:
                    log.error("прогон остановлен: %s", exc)
                    outcomes.append(
                        ApplyOutcome(vacancy.id, vacancy.name, vacancy.employer, vacancy.url,
                                     "failed", f"лимит hh.ru: {exc}")
                    )
                    return outcomes

                outcomes.append(outcome)
                if outcome.status in {"applied", "dry_run", "prepared"}:
                    sent += 1
                    log.info("[%s] %s — %s (%s)", outcome.status, vacancy.name, vacancy.employer,
                             outcome.fit_score)
                    if outcome.status == "applied":
                        self._sleep_between_applications()
                else:
                    log.info("[пропуск] %s — %s", vacancy.name, outcome.reason)

        return outcomes

    # ---------- переписка ----------

    def prepare_applications(self, limit: int | None = None) -> list[ApplyOutcome]:
        return self.run_applications(limit=limit, mode="prepare")

    def run_chat(self, limit: int | None = None) -> list[ChatOutcome]:
        agent = ChatAgent(self.config, self.client, self.llm, self.storage)
        return agent.run(limit=limit)

    # ---------- всё сразу ----------

    def run_all(self) -> RunReport:
        report = RunReport()
        report.chats = self.run_chat()  # сначала отвечаем людям, потом ищем новое
        report.applications = self.run_applications()
        return report


def render_letters_markdown(outcomes: list[ApplyOutcome]) -> str:
    """Готовые письма в Markdown: открыть ссылку, скопировать текст, нажать «Откликнуться»."""
    ready = [o for o in outcomes if o.letter and o.status in {"prepared", "dry_run", "applied"}]
    lines = [
        f"# Отклики на hh.ru — {date.today().isoformat()}",
        "",
        f"Готово писем: {len(ready)}. Порядок: открыть ссылку → «Откликнуться» → "
        "вставить письмо → отправить.",
        "",
    ]
    for index, outcome in enumerate(ready, 1):
        lines += [
            f"## {index}. {outcome.title} — {outcome.employer}",
            "",
            f"* Оценка соответствия: **{outcome.fit_score if outcome.fit_score is not None else '—'}**",
            f"* Ссылка: {outcome.url or '—'}",
        ]
        if outcome.screen_notes:
            lines.append("* Почему подходит: " + "; ".join(outcome.screen_notes))
        lines += ["", "```", outcome.letter.strip(), "```", ""]
    return "\n".join(lines)
