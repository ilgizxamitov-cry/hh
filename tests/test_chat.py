from datetime import datetime
from zoneinfo import ZoneInfo

from hhbot.chat import ChatAgent
from hhbot.models import Negotiation
from tests.fakes import FakeHhClient, FakeLLM, employer_message, negotiation_payload, slot_analysis

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=MSK)  # вторник


def build(config, storage, llm, messages):
    client = FakeHhClient(
        negotiations=[negotiation_payload()], messages={"55": messages}
    )
    return ChatAgent(config, client, llm, storage), client


def test_confirms_slot_inside_availability(config, storage):
    config.chat.autopilot = True
    config.chat.auto_confirm_interviews = True
    config.dry_run = False
    llm = FakeLLM(analysis=slot_analysis("2026-09-03T15:00:00+03:00"))
    agent, client = build(config, storage, llm,
                          [employer_message("m1", "Приглашаем в четверг в 15:00, удобно?")])

    outcome = agent.handle(Negotiation.from_api(negotiation_payload()), now=NOW)

    assert outcome.action == "sent"
    assert not outcome.needs_human
    assert client.sent_messages and client.sent_messages[0][0] == "55"

    decision = [c for c in llm.calls if c["method"] == "compose_reply"][0]["slot_decision"]
    assert decision.startswith("ПОДТВЕРДИТЬ")

    interviews = storage.list_interviews()
    assert len(interviews) == 1
    assert interviews[0]["status"] == "confirmed"
    assert interviews[0]["starts_at"].startswith("2026-09-03T15:00")


def test_confirmation_needs_human_when_auto_confirm_disabled(config, storage):
    config.chat.autopilot = True
    config.chat.auto_confirm_interviews = False
    config.dry_run = False
    llm = FakeLLM(analysis=slot_analysis("2026-09-03T15:00:00+03:00"))
    agent, client = build(config, storage, llm, [employer_message("m1", "Ждём в четверг в 15:00")])

    outcome = agent.handle(Negotiation.from_api(negotiation_payload()), now=NOW)

    assert outcome.action == "draft" and outcome.needs_human
    assert client.sent_messages == []
    assert storage.list_drafts()[0]["needs_human"] == 1
    assert storage.list_interviews()[0]["status"] == "proposed"


def test_proposes_own_slots_when_time_does_not_fit(config, storage):
    config.chat.autopilot = True
    config.chat.auto_confirm_interviews = True
    config.dry_run = False
    llm = FakeLLM(analysis=slot_analysis("2026-09-06T22:00:00+03:00"))  # воскресенье, ночь
    agent, _ = build(config, storage, llm, [employer_message("m1", "Давайте в воскресенье в 22:00")])

    agent.handle(Negotiation.from_api(negotiation_payload()), now=NOW)

    decision = [c for c in llm.calls if c["method"] == "compose_reply"][0]["slot_decision"]
    assert "ПРЕДЛОЖИТЬ свои варианты" in decision
    assert "Europe/Moscow" in decision
    assert storage.list_interviews() == []


def test_escalates_on_stop_word(config, storage):
    config.chat.autopilot = True
    config.dry_run = False
    llm = FakeLLM()
    agent, client = build(config, storage, llm,
                          [employer_message("m1", "Готовы обсудить оффер и оклад")])

    outcome = agent.handle(Negotiation.from_api(negotiation_payload()), now=NOW)

    assert outcome.needs_human and client.sent_messages == []
    assert "стоп-слово" in outcome.reason


def test_escalates_on_test_task_intent(config, storage):
    config.chat.autopilot = True
    config.dry_run = False
    llm = FakeLLM(analysis=slot_analysis("2026-09-03T15:00:00+03:00", intent="test_task"))
    agent, client = build(config, storage, llm, [employer_message("m1", "Вот задание")])

    outcome = agent.handle(Negotiation.from_api(negotiation_payload()), now=NOW)

    assert outcome.needs_human and client.sent_messages == []


def test_dry_run_never_sends(config, storage):
    config.chat.autopilot = True
    config.chat.auto_confirm_interviews = True
    config.dry_run = True
    llm = FakeLLM(analysis=slot_analysis("2026-09-03T15:00:00+03:00"))
    agent, client = build(config, storage, llm, [employer_message("m1", "В четверг в 15:00?")])

    outcome = agent.handle(Negotiation.from_api(negotiation_payload()), now=NOW)

    assert outcome.action == "dry_run" and client.sent_messages == []


def test_message_processed_only_once(config, storage):
    llm = FakeLLM()
    agent, _ = build(config, storage, llm, [employer_message("m1", "Расскажите про опыт")])
    negotiation = Negotiation.from_api(negotiation_payload())

    first = agent.handle(negotiation, now=NOW)
    second = agent.handle(negotiation, now=NOW)

    assert first.draft_id is not None
    assert second.action == "skipped" and second.reason == "новых сообщений нет"


def test_daily_reply_limit_blocks_sending(config, storage):
    config.chat.autopilot = True
    config.dry_run = False
    config.limits.max_chat_replies_per_day = 1
    storage.bump("chat_replies")
    llm = FakeLLM()
    agent, client = build(config, storage, llm, [employer_message("m1", "Когда можете созвониться?")])

    outcome = agent.handle(Negotiation.from_api(negotiation_payload()), now=NOW)

    assert outcome.action == "draft" and client.sent_messages == []


def test_run_skips_negotiations_without_updates(config, storage):
    llm = FakeLLM()
    client = FakeHhClient(
        negotiations=[negotiation_payload(has_updates=False, counters={"unread_messages": 0})],
        messages={"55": [employer_message("m1", "привет")]},
    )
    agent = ChatAgent(config, client, llm, storage)
    assert agent.run() == []


def test_plan_reply_works_without_hh_client(config, storage):
    """Офлайн-разбор переписки: клиент hh.ru не нужен (`hhbot reply`)."""
    config.chat.auto_confirm_interviews = True
    llm = FakeLLM(analysis=slot_analysis("2026-09-03T15:00:00+03:00"))
    agent = ChatAgent(config, None, llm, storage)

    plan = agent.plan_reply(
        vacancy_title="Python-разработчик",
        employer="ACME",
        new_messages="Приглашаем в четверг в 15:00",
        now=NOW,
    )

    assert plan.decision.kind == "confirm"
    assert not plan.needs_human
    assert plan.reply
    assert plan.decision.slot is not None


def test_plan_reply_escalates_offline(config, storage):
    agent = ChatAgent(config, None, FakeLLM(), storage)
    plan = agent.plan_reply(
        vacancy_title="Python", employer="ACME",
        new_messages="Пришлите скан паспорта для оформления", now=NOW,
    )
    assert plan.needs_human and "стоп-слово" in "; ".join(plan.escalations)
