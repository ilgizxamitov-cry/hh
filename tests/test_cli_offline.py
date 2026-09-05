"""Офлайн-команды CLI: работают без сети и без доступа к API hh.ru."""

from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner

from hhbot import cli
from hhbot.llm import ProposedSlot
from tests.fakes import FakeLLM, slot_analysis

runner = CliRunner()


@pytest.fixture()
def workdir(tmp_path, monkeypatch, profile, availability):
    """Каталог с config.yaml и без каких-либо ключей hh.ru."""
    config = {
        "dry_run": True,
        "db_path": str(tmp_path / "hhbot.db"),
        "profile": profile.model_dump(mode="json"),
        "availability": availability.model_dump(mode="json"),
        "letter": {"min_chars": 50, "max_chars": 800},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for var in ("HH_CLIENT_ID", "HH_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def use_llm(monkeypatch, llm: FakeLLM) -> None:
    monkeypatch.setattr(cli.Context, "llm", property(lambda self: llm))


def test_letter_command_writes_letter(workdir, monkeypatch):
    llm = FakeLLM(letter_body="В ACME нужен Python-разработчик, готов подключиться к задачам. " * 3)
    use_llm(monkeypatch, llm)
    (workdir / "vacancy.txt").write_text("Нужен Python-разработчик с FastAPI", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["letter", "-f", "vacancy.txt", "--title", "Python-разработчик",
         "--employer", "ACME", "-o", "letter.txt"],
    )

    assert result.exit_code == 0, result.output
    assert "Оценка соответствия: 85" in result.output
    assert (workdir / "letter.txt").read_text(encoding="utf-8").startswith("В ACME")
    assert [c["method"] for c in llm.calls] == ["screen_vacancy", "write_cover_letter"]


def test_letter_command_rejects_empty_file(workdir, monkeypatch):
    use_llm(monkeypatch, FakeLLM())
    (workdir / "empty.txt").write_text("   ", encoding="utf-8")

    result = runner.invoke(cli.app, ["letter", "-f", "empty.txt"])

    assert result.exit_code != 0


def test_reply_command_plans_slot_and_saves_draft(workdir, monkeypatch):
    llm = FakeLLM(
        analysis=slot_analysis("2099-09-03T15:00:00+03:00"),
        reply_text="Спасибо, подтверждаю четверг в 15:00.",
    )
    use_llm(monkeypatch, llm)
    (workdir / "chat.txt").write_text("Приглашаем на собеседование в четверг в 15:00", encoding="utf-8")

    result = runner.invoke(
        cli.app, ["reply", "-f", "chat.txt", "--employer", "ACME", "--vacancy", "Python", "--save"]
    )

    assert result.exit_code == 0, result.output
    assert "Решение по времени" in result.output
    assert "Черновик #1 сохранён" in result.output


def test_reply_command_reports_escalation(workdir, monkeypatch):
    llm = FakeLLM(
        analysis=slot_analysis("", proposed_slots=[ProposedSlot(start_iso="", end_iso="", raw="")],
                               intent="offer", needs_human=True,
                               escalation_reason="обсуждение оффера"),
    )
    use_llm(monkeypatch, llm)
    (workdir / "chat.txt").write_text("Готовы обсудить оффер и зарплату", encoding="utf-8")

    result = runner.invoke(cli.app, ["reply", "-f", "chat.txt", "--employer", "ACME"])

    assert result.exit_code == 0, result.output
    assert "Требует вашего решения" in result.output


def test_doctor_survives_missing_hh_keys(workdir, monkeypatch):
    """Без ключей hh.ru doctor должен отработать и подсказать офлайн-путь, а не упасть."""
    result = runner.invoke(cli.app, ["doctor"])

    assert "HH_CLIENT_ID" in result.output
    assert result.exit_code == 1  # ключей нет — это ожидаемый провал проверок
