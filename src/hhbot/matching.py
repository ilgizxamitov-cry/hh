"""Отбор вакансий: жёсткие фильтры и предварительная оценка соответствия.

Дешёвая детерминированная логика отсекает заведомо неподходящее до обращения
к модели — так расходуется меньше токенов и меньше запросов к hh.ru.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import FiltersConfig, ProfileConfig
from .models import Vacancy


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""


def _text_blob(vacancy: Vacancy) -> str:
    return " ".join(
        [vacancy.name, vacancy.employer, vacancy.description, " ".join(vacancy.key_skills)]
    ).lower()


def hard_filter(vacancy: Vacancy, filters: FiltersConfig, profile: ProfileConfig) -> FilterResult:
    """Причина отказа возвращается человекочитаемой строкой — она попадает в лог и БД."""
    if vacancy.archived:
        return FilterResult(False, "вакансия в архиве")
    if vacancy.already_responded:
        return FilterResult(False, "отклик уже отправлен ранее")
    if filters.skip_with_test and vacancy.has_test:
        return FilterResult(False, "требуется тест hh.ru — отклик нужно отправлять вручную")
    if filters.skip_agencies and vacancy.is_agency:
        return FilterResult(False, "кадровое агентство")

    blob = _text_blob(vacancy)

    for word in filters.exclude_keywords:
        if word.lower() in blob:
            return FilterResult(False, f"стоп-слово «{word}»")

    for employer in filters.exclude_employers:
        if employer.lower() in vacancy.employer.lower():
            return FilterResult(False, f"компания в чёрном списке: {vacancy.employer}")

    if filters.require_keywords_any and not any(
        word.lower() in blob for word in filters.require_keywords_any
    ):
        return FilterResult(False, "нет ни одного обязательного ключевого слова")

    if filters.allowed_experience and vacancy.experience and (
        vacancy.experience not in filters.allowed_experience
    ):
        return FilterResult(False, f"требуемый опыт «{vacancy.experience}» вне заданного диапазона")

    if filters.max_age_days and (age := vacancy.age_days) is not None and age > filters.max_age_days:
        return FilterResult(False, f"вакансия старше {filters.max_age_days} дней")

    if filters.min_salary is not None:
        upper = vacancy.salary_to or vacancy.salary_from
        if upper is None:
            if not filters.treat_missing_salary_as_ok:
                return FilterResult(False, "зарплата не указана")
        else:
            if vacancy.salary_currency and vacancy.salary_currency != filters.salary_currency:
                # чужая валюта — не сравниваем вслепую, отдаём на оценку модели
                pass
            elif upper < filters.min_salary:
                return FilterResult(False, f"потолок зарплаты {upper} < {filters.min_salary}")

    if profile.remote_only and vacancy.schedule and vacancy.schedule != "remote":
        return FilterResult(False, "нужна удалённая работа, а график другой")

    return FilterResult(True)


def prescore(vacancy: Vacancy, profile: ProfileConfig) -> int:
    """Грубая оценка 0-100 без обращения к модели: пересечение навыков, зарплата, свежесть."""
    score = 40

    skills = {s.lower() for s in profile.key_skills}
    if skills:
        blob = _text_blob(vacancy)
        hits = sum(1 for skill in skills if skill in blob)
        score += min(35, int(35 * hits / max(3, len(skills) * 0.6)))

    if profile.salary_expectation:
        upper = vacancy.salary_to or vacancy.salary_from
        if upper:
            ratio = upper / profile.salary_expectation
            if ratio >= 1.2:
                score += 15
            elif ratio >= 1.0:
                score += 10
            elif ratio >= 0.85:
                score += 3
            else:
                score -= 15

    if profile.remote_only and vacancy.schedule == "remote":
        score += 5

    age = vacancy.age_days
    if age is not None and age <= 2:
        score += 5

    return max(0, min(100, score))


def rank(vacancies: list[Vacancy], profile: ProfileConfig) -> list[Vacancy]:
    return sorted(vacancies, key=lambda v: prescore(v, profile), reverse=True)
