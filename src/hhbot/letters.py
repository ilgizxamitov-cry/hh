"""Сборка и проверка сопроводительного письма перед отправкой."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import LetterConfig
from .llm import LLM, CoverLetter, VacancyScreen
from .models import Vacancy

PLACEHOLDER_RE = re.compile(r"[\[\{<](?:[^\]\}>]{0,60})[\]\}>]")
BANNED_PATTERNS = [
    re.compile(r"как\s+(?:языковая\s+)?модель", re.I),
    re.compile(r"\bИИ[- ]ассистент", re.I),
    re.compile(r"\bнейросет", re.I),
    re.compile(r"\bChatGPT|\bClaude\b", re.I),
    re.compile(r"ваша\s+компания\s+—?\s*лидер\s+рынка", re.I),
]


@dataclass
class LetterCheck:
    ok: bool
    problems: list[str]


def check_letter(text: str, config: LetterConfig, vacancy: Vacancy) -> LetterCheck:
    problems: list[str] = []
    stripped = text.strip()

    if len(stripped) > config.max_chars:
        problems.append(f"длина {len(stripped)} > лимита {config.max_chars}")
    if len(stripped) < config.min_chars:
        problems.append(f"длина {len(stripped)} < минимума {config.min_chars}")

    for match in PLACEHOLDER_RE.finditer(stripped):
        token = match.group(0)
        # ссылки и «(2)» плейсхолдерами не считаем
        if len(token) > 4 and not token.startswith("<http"):
            problems.append(f"похоже на незаполненный плейсхолдер: {token}")
            break

    for pattern in BANNED_PATTERNS:
        if pattern.search(stripped):
            problems.append(f"запрещённая формулировка: {pattern.pattern}")

    for phrase in config.must_mention:
        if phrase.lower() not in stripped.lower():
            problems.append(f"не упомянуто обязательное: {phrase}")

    if vacancy.employer and vacancy.employer.lower() not in stripped.lower():
        # не ошибка, но полезный сигнал: письмо выглядит обезличенным
        if len(stripped) > 0 and vacancy.name.split()[0].lower() not in stripped.lower():
            problems.append("письмо не привязано ни к компании, ни к названию вакансии")

    return LetterCheck(not problems, problems)


def build_cover_letter(
    llm: LLM,
    vacancy: Vacancy,
    screen: VacancyScreen,
    config: LetterConfig,
    max_attempts: int = 2,
) -> tuple[str, LetterCheck]:
    """Генерирует письмо и один раз просит переписать, если проверки не прошли."""
    letter: CoverLetter | None = None
    check = LetterCheck(False, ["письмо не сгенерировано"])

    for attempt in range(max_attempts):
        letter = llm.write_cover_letter(vacancy, screen, config)
        check = check_letter(letter.body, config, vacancy)
        if check.ok:
            break
        if attempt + 1 < max_attempts:
            # усиливаем ограничения и пробуем ещё раз
            config = config.model_copy(
                update={
                    "must_mention": config.must_mention,
                    "forbid": config.forbid
                    + [f"исправь замечания предыдущей версии: {'; '.join(check.problems)}"],
                }
            )

    body = (letter.body if letter else "").strip()
    if config.signature and config.signature not in body:
        body = f"{body}\n\n{config.signature}"
    return body, check
