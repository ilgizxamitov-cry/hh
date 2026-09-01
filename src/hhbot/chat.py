"""Переписка с работодателем и запись на собеседования.

Ключевой принцип: решение о времени принимает КОД (по календарю доступности), а не модель.
Модель только извлекает факты из сообщения и формулирует текст ответа под готовое решение.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .api import HhApiError, HhClient
from .config import BotConfig
from .llm import LLM, MessageAnalysis
from .models import Message, Negotiation
from .scheduling import Scheduler, Slot
from .storage import Storage

log = logging.getLogger(__name__)

# Намерения, при которых бот никогда не отвечает сам
ALWAYS_ESCALATE_INTENTS = {"offer", "test_task", "rejection", "spam"}


@dataclass
class SlotDecision:
    kind: str  # confirm | propose | none | out_of_reach
    text: str  # формулировка решения для модели
    slot: Slot | None = None
    proposed: list[Slot] = field(default_factory=list)


@dataclass
class ChatOutcome:
    negotiation_id: str
    vacancy: str
    employer: str
    intent: str = ""
    action: str = "skipped"  # sent | draft | skipped | error | dry_run
    needs_human: bool = False
    reason: str = ""
    draft_id: int | None = None
    reply: str = ""
    interview: Slot | None = None


class ChatAgent:
    def __init__(self, config: BotConfig, client: HhClient, llm: LLM, storage: Storage) -> None:
        self.config = config
        self.client = client
        self.llm = llm
        self.storage = storage
        self.tz = ZoneInfo(config.availability.timezone)

    # ---------- вспомогательное ----------

    def _scheduler(self) -> Scheduler:
        # занятость перечитываем каждый раз: между итерациями могли добавиться встречи
        return Scheduler(self.config.availability, busy=self.storage.busy_intervals())

    def _keyword_escalation(self, text: str) -> str:
        lowered = text.lower()
        for word in self.config.chat.escalate_keywords:
            if word.lower() in lowered:
                return f"в сообщении есть стоп-слово «{word}»"
        return ""

    def _decide_slot(self, analysis: MessageAnalysis, scheduler: Scheduler,
                     now: datetime) -> SlotDecision:
        """Сопоставляет предложенное работодателем время с календарём доступности."""
        needs_time = (
            analysis.intent in {"invitation", "logistics"}
            or analysis.asks_availability
            or bool(analysis.proposed_slots)
        )
        if not needs_time:
            return SlotDecision("none", "вопрос о времени не поднимался — про время не пиши")

        rejected: list[str] = []
        for proposal in analysis.proposed_slots:
            if not proposal.start_iso:
                continue
            try:
                start = datetime.fromisoformat(proposal.start_iso)
            except ValueError:
                log.warning("не разобрал время из сообщения: %r", proposal.start_iso)
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=self.tz)
            duration = None
            if proposal.end_iso:
                try:
                    end = datetime.fromisoformat(proposal.end_iso)
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=self.tz)
                    duration = max(15, int((end - start).total_seconds() // 60))
                except ValueError:
                    duration = None
            ok, why = scheduler.can_accept(start, duration, now=now)
            if ok:
                slot = Slot(
                    start.astimezone(self.tz),
                    start.astimezone(self.tz)
                    + timedelta(minutes=duration or self.config.availability.slot_minutes),
                )
                return SlotDecision(
                    "confirm",
                    f"ПОДТВЕРДИТЬ время: {slot.human()}. Это время свободно у соискателя.",
                    slot=slot,
                )
            rejected.append(f"{proposal.raw or proposal.start_iso} — {why}")

        free = scheduler.free_slots(limit=3, now=now)
        if not free:
            return SlotDecision(
                "out_of_reach",
                "Свободных слотов в ближайшее время нет. Напиши, что вернёшься с вариантами "
                "времени в ближайшее время, конкретные даты не называй.",
            )

        options = "; ".join(slot.human() for slot in free)
        prefix = (
            f"Предложенное работодателем время не подходит ({'; '.join(rejected)}). "
            if rejected
            else ""
        )
        return SlotDecision(
            "propose",
            f"{prefix}ПРЕДЛОЖИТЬ свои варианты (перечисли все, ровно в таком виде): {options}",
            proposed=free,
        )

    @staticmethod
    def _format_messages(messages: list[Message]) -> str:
        return "\n\n".join(
            f"[{m.created_at}] {'Работодатель' if m.from_employer else 'Соискатель'}: {m.text}"
            for m in messages
        )

    # ---------- основной сценарий ----------

    def handle(self, negotiation: Negotiation, now: datetime | None = None) -> ChatOutcome:
        outcome = ChatOutcome(
            negotiation_id=negotiation.id,
            vacancy=negotiation.vacancy_name,
            employer=negotiation.employer,
        )
        try:
            raw_messages = self.client.messages(negotiation.id)
        except HhApiError as exc:
            outcome.action, outcome.reason = "error", str(exc)
            return outcome

        messages = [Message.from_api(m) for m in raw_messages]
        messages.sort(key=lambda m: m.created_at)
        new_from_employer = [
            m for m in messages if m.from_employer and not self.storage.is_message_seen(m.id)
        ]
        if not new_from_employer:
            outcome.reason = "новых сообщений нет"
            return outcome

        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        new_ids = {m.id for m in new_from_employer}
        history = self._format_messages([m for m in messages if m.id not in new_ids][-12:])
        new_text = self._format_messages(new_from_employer)

        analysis = self.llm.analyze_message(
            vacancy_title=negotiation.vacancy_name,
            employer=negotiation.employer,
            state=negotiation.state,
            history=history,
            new_messages=new_text,
            now=now,
            timezone_name=self.config.availability.timezone,
        )
        outcome.intent = analysis.intent

        scheduler = self._scheduler()
        decision = self._decide_slot(analysis, scheduler, now)

        # --- решаем, можно ли отвечать автоматически ---
        escalations: list[str] = []
        if analysis.needs_human:
            escalations.append(analysis.escalation_reason or "модель пометила как требующее человека")
        if analysis.intent in ALWAYS_ESCALATE_INTENTS:
            escalations.append(f"тема «{analysis.intent}» решается только человеком")
        if keyword := self._keyword_escalation(" ".join(m.text for m in new_from_employer)):
            escalations.append(keyword)
        if decision.kind == "confirm" and not self.config.chat.auto_confirm_interviews:
            escalations.append("подтверждение времени требует вашего согласия (auto_confirm_interviews=false)")

        outcome.needs_human = bool(escalations)
        outcome.reason = "; ".join(escalations)

        draft = self.llm.compose_reply(
            vacancy_title=negotiation.vacancy_name,
            employer=negotiation.employer,
            new_messages=new_text,
            analysis=analysis,
            slot_decision=decision.text,
            now=now,
            timezone_name=self.config.availability.timezone,
            language=self.config.chat.reply_language,
            max_chars=self.config.chat.max_reply_chars,
            signature=self.config.profile.full_name,
        )
        reply = draft.text.strip()[: self.config.chat.max_reply_chars]
        outcome.reply = reply

        can_send = (
            self.config.chat.autopilot
            and not outcome.needs_human
            and not self.config.dry_run
            and self.storage.count("chat_replies") < self.config.limits.max_chat_replies_per_day
        )

        status = "pending"
        if can_send:
            try:
                self.client.send_message(negotiation.id, reply)
                status = "sent"
                outcome.action = "sent"
                self.storage.bump("chat_replies")
            except HhApiError as exc:
                status = "pending"
                outcome.action = "error"
                outcome.reason = f"ошибка отправки: {exc}"
        else:
            outcome.action = "dry_run" if self.config.dry_run else "draft"

        draft_id = self.storage.add_draft(
            negotiation_id=negotiation.id,
            text=reply,
            intent=analysis.intent,
            status=status,
            needs_human=outcome.needs_human,
            note=outcome.reason,
            vacancy_title=negotiation.vacancy_name,
            employer=negotiation.employer,
            reply_to=new_from_employer[-1].id,
            meta={
                "slot_decision": decision.kind,
                "summary": analysis.summary,
                "questions": analysis.questions,
                "format": analysis.interview_format,
                "location": analysis.location,
                "contact": analysis.contact,
            },
        )
        outcome.draft_id = draft_id

        # --- фиксируем встречу ---
        if decision.kind == "confirm" and decision.slot:
            outcome.interview = decision.slot
            starts, ends = decision.slot.to_iso()
            self.storage.add_interview(
                negotiation_id=negotiation.id,
                starts_at=starts,
                ends_at=ends,
                status="confirmed" if status == "sent" else "proposed",
                vacancy_title=negotiation.vacancy_name,
                employer=negotiation.employer,
                fmt=analysis.interview_format or "online",
                location=analysis.location,
                contact=analysis.contact,
            )

        # сообщения помечаем обработанными, только если ответ ушёл или лёг в черновики,
        # иначе при следующем запуске бот забудет о них
        for message in new_from_employer:
            self.storage.mark_message_seen(
                {"id": message.id, "created_at": message.created_at, "text": message.text,
                 "author": {"participant_type": message.author}},
                negotiation.id,
            )
        return outcome

    def run(self, limit: int | None = None) -> list[ChatOutcome]:
        """Обходит переписки с обновлениями и обрабатывает новые сообщения."""
        outcomes: list[ChatOutcome] = []
        items = self.client.negotiations()
        for raw in items:
            negotiation = Negotiation.from_api(raw)
            if not (negotiation.has_updates or negotiation.unread):
                continue
            if limit is not None and len(outcomes) >= limit:
                break
            log.info("Переписка %s — %s (%s)", negotiation.id, negotiation.vacancy_name,
                     negotiation.employer)
            outcomes.append(self.handle(negotiation))
        return outcomes

    # ---------- отправка отложенного черновика ----------

    def send_draft(self, draft_id: int) -> ChatOutcome:
        row = self.storage.get_draft(draft_id)
        if row is None:
            raise KeyError(f"черновик {draft_id} не найден")
        outcome = ChatOutcome(
            negotiation_id=row["negotiation_id"],
            vacancy=row["vacancy_title"] or "",
            employer=row["employer"] or "",
            intent=row["intent"] or "",
            reply=row["text"],
        )
        if row["status"] == "sent":
            outcome.action, outcome.reason = "skipped", "уже отправлен"
            return outcome
        if self.config.dry_run:
            outcome.action, outcome.reason = "dry_run", "включён dry_run — ничего не отправлено"
            return outcome
        self.client.send_message(row["negotiation_id"], row["text"])
        self.storage.set_draft_status(draft_id, "sent")
        self.storage.bump("chat_replies")
        outcome.action = "sent"
        return outcome
