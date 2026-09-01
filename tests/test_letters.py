from hhbot.config import LetterConfig
from hhbot.letters import build_cover_letter, check_letter
from hhbot.models import Vacancy
from tests.fakes import FakeLLM
from tests.conftest import vacancy_payload


def vacancy() -> Vacancy:
    return Vacancy.from_api(vacancy_payload())


def test_rejects_too_long_letter():
    result = check_letter("а" * 3000, LetterConfig(max_chars=1400, min_chars=10), vacancy())
    assert not result.ok and any("длина" in p for p in result.problems)


def test_rejects_placeholders():
    text = "Здравствуйте! Хочу работать в [название компании] на позиции Python-разработчик. " * 6
    result = check_letter(text, LetterConfig(min_chars=10, max_chars=2000), vacancy())
    assert any("плейсхолдер" in p for p in result.problems)


def test_rejects_ai_disclosure():
    text = "Как языковая модель, я подготовила это письмо для ACME Python-разработчик. " * 8
    result = check_letter(text, LetterConfig(min_chars=10, max_chars=2000), vacancy())
    assert any("запрещённая формулировка" in p for p in result.problems)


def test_requires_must_mention():
    text = "В ACME на позиции Python-разработчик могу закрыть задачи по бэкенду. " * 8
    config = LetterConfig(min_chars=10, max_chars=2000, must_mention=["готов к релокации"])
    assert any("обязательное" in p for p in check_letter(text, config, vacancy()).problems)


def test_flags_impersonal_letter():
    text = "Добрый день. Имею большой опыт в разработке и готов обсудить задачи команды. " * 6
    result = check_letter(text, LetterConfig(min_chars=10, max_chars=2000), vacancy())
    assert any("не привязано" in p for p in result.problems)


def test_accepts_good_letter():
    text = (
        "В вакансии ACME важен опыт с FastAPI — последние три года пишу на нём сервисы, "
        "которые держат пиковую нагрузку. Ускорил ключевое API в шесть раз за счёт кэша и "
        "переработки запросов к PostgreSQL. Готов обсудить задачи команды."
    )
    assert check_letter(text, LetterConfig(min_chars=100, max_chars=2000), vacancy()).ok


def test_build_retries_and_appends_signature():
    llm = FakeLLM(letter_body="В ACME нужен Python-разработчик, готов подключиться. " * 4)
    config = LetterConfig(min_chars=50, max_chars=2000, signature="Иван Иванов, +7 900 000-00-00")

    body, check = build_cover_letter(llm, vacancy(), llm.screen_vacancy(vacancy()), config)

    assert check.ok
    assert body.endswith("Иван Иванов, +7 900 000-00-00")
    assert len([c for c in llm.calls if c["method"] == "write_cover_letter"]) == 1


def test_build_retries_when_letter_is_bad():
    llm = FakeLLM(letter_body="слишком коротко")
    config = LetterConfig(min_chars=500, max_chars=2000)

    _, check = build_cover_letter(llm, vacancy(), llm.screen_vacancy(vacancy()), config)

    assert not check.ok
    assert len([c for c in llm.calls if c["method"] == "write_cover_letter"]) == 2
