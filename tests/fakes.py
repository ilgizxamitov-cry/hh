"""Заглушки внешних сервисов для тестов."""

from __future__ import annotations

from typing import Any, Iterator

from hhbot.llm import CoverLetter, MessageAnalysis, ProposedSlot, ReplyDraft, VacancyScreen


class FakeLLM:
    """Подменяет hhbot.llm.LLM: возвращает заранее заданные структуры."""

    def __init__(
        self,
        analysis: MessageAnalysis | None = None,
        screen: VacancyScreen | None = None,
        letter_body: str = "",
        reply_text: str = "Спасибо, подтверждаю.",
    ) -> None:
        self.analysis = analysis
        self.screen = screen
        self.letter_body = letter_body or ("Готов помочь с задачами в ACME. " * 8)
        self.reply_text = reply_text
        self.calls: list[dict[str, Any]] = []

    def screen_vacancy(self, vacancy):
        self.calls.append({"method": "screen_vacancy", "vacancy": vacancy.id})
        return self.screen or VacancyScreen(
            fit_score=85, apply=True, reasons=["стек совпадает"],
            matched_skills=["Python"], gaps=[], hooks=["опыт с FastAPI"], red_flags=[],
        )

    def write_cover_letter(self, vacancy, screen, letter):
        self.calls.append({"method": "write_cover_letter", "vacancy": vacancy.id})
        return CoverLetter(
            body=self.letter_body.replace("ACME", vacancy.employer or "ACME"),
            used_facts=["опыт 5 лет"],
            self_check="выдумок нет",
        )

    def analyze_message(self, **kwargs):
        self.calls.append({"method": "analyze_message", **kwargs})
        return self.analysis or MessageAnalysis(
            intent="question", summary="вопрос про опыт", proposed_slots=[],
            asks_availability=False, questions=["Какой опыт с Python?"],
            answerable_from_profile=["5 лет"], needs_human=False, escalation_reason="",
            interview_format="", location="", contact="",
        )

    def compose_reply(self, **kwargs):
        self.calls.append({"method": "compose_reply", **kwargs})
        return ReplyDraft(text=self.reply_text, self_check="ок")


class FakeHhClient:
    """Подменяет hhbot.api.HhClient."""

    def __init__(
        self,
        negotiations: list[dict] | None = None,
        messages: dict[str, list[dict]] | None = None,
        vacancies: list[dict] | None = None,
        resumes: list[dict] | None = None,
    ) -> None:
        self._negotiations = negotiations or []
        self._messages = messages or {}
        self._vacancies = {str(v["id"]): v for v in (vacancies or [])}
        self._resumes = resumes if resumes is not None else [{"id": "res-1", "title": "Python"}]
        self.sent_messages: list[tuple[str, str]] = []
        self.applications: list[tuple[str, str, str | None]] = []

    # --- API, которое использует бот ---

    def my_resumes(self) -> list[dict]:
        return self._resumes

    def negotiations(self, **kwargs: Any) -> list[dict]:
        return self._negotiations

    def messages(self, negotiation_id: str, **kwargs: Any) -> list[dict]:
        return self._messages.get(str(negotiation_id), [])

    def send_message(self, negotiation_id: str, text: str) -> dict:
        self.sent_messages.append((str(negotiation_id), text))
        return {}

    def search_vacancies(self, params: dict, max_pages: int = 3) -> Iterator[dict]:
        yield from ({"id": vid} for vid in self._vacancies)

    def get_vacancy(self, vacancy_id: str) -> dict:
        return self._vacancies[str(vacancy_id)]

    def apply(self, vacancy_id: str, resume_id: str, message: str | None = None) -> dict:
        self.applications.append((str(vacancy_id), str(resume_id), message))
        return {}


def employer_message(message_id: str, text: str, created_at: str = "2026-09-01T10:00:00+0300") -> dict:
    return {
        "id": message_id,
        "text": text,
        "created_at": created_at,
        "author": {"participant_type": "employer"},
    }


def negotiation_payload(negotiation_id: str = "55", **overrides) -> dict:
    payload = {
        "id": negotiation_id,
        "state": {"id": "response"},
        "has_updates": True,
        "counters": {"unread_messages": 1},
        "vacancy": {
            "id": "1001",
            "name": "Python-разработчик",
            "employer": {"name": "ACME"},
            "alternate_url": "https://hh.ru/vacancy/1001",
        },
    }
    payload.update(overrides)
    return payload


def slot_analysis(start_iso: str, **overrides) -> MessageAnalysis:
    data = dict(
        intent="invitation",
        summary="приглашение на собеседование",
        proposed_slots=[ProposedSlot(start_iso=start_iso, end_iso="", raw=start_iso)],
        asks_availability=False,
        questions=[],
        answerable_from_profile=[],
        needs_human=False,
        escalation_reason="",
        interview_format="online",
        location="Google Meet",
        contact="Мария, HR",
    )
    data.update(overrides)
    return MessageAnalysis(**data)
