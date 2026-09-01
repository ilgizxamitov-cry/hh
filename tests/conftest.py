from __future__ import annotations

from datetime import time

import pytest

from hhbot.config import (
    AvailabilityConfig, BotConfig, ChatConfig, FiltersConfig, LetterConfig,
    LimitsConfig, ProfileConfig, TimeWindow,
)
from hhbot.storage import Storage


@pytest.fixture()
def profile() -> ProfileConfig:
    return ProfileConfig(
        full_name="Иван Иванов",
        headline="Python-разработчик",
        years_experience=5,
        key_skills=["Python", "FastAPI", "PostgreSQL"],
        achievements=["Ускорил API в 6 раз"],
        salary_expectation=300000,
        city="Москва",
    )


@pytest.fixture()
def availability() -> AvailabilityConfig:
    return AvailabilityConfig(
        timezone="Europe/Moscow",
        weekly={
            0: [TimeWindow(start=time(11, 0), end=time(18, 0))],
            1: [TimeWindow(start=time(11, 0), end=time(18, 0))],
            2: [TimeWindow(start=time(11, 0), end=time(18, 0))],
            3: [TimeWindow(start=time(11, 0), end=time(18, 0))],
            4: [TimeWindow(start=time(11, 0), end=time(16, 0))],
        },
        min_notice_hours=12,
        buffer_minutes=30,
        slot_minutes=60,
        max_per_day=2,
    )


@pytest.fixture()
def storage(tmp_path) -> Storage:
    return Storage(tmp_path / "test.db")


@pytest.fixture()
def config(tmp_path, profile, availability) -> BotConfig:
    return BotConfig(
        profile=profile,
        availability=availability,
        filters=FiltersConfig(min_fit_score=70, min_salary=200000),
        letter=LetterConfig(min_chars=50, max_chars=800),
        chat=ChatConfig(autopilot=False, auto_confirm_interviews=False),
        limits=LimitsConfig(max_applications_per_run=5, min_delay_seconds=0, max_delay_seconds=0),
        dry_run=True,
        db_path=tmp_path / "test.db",
    )


def vacancy_payload(**overrides) -> dict:
    payload = {
        "id": "1001",
        "name": "Python-разработчик",
        "employer": {"id": "77", "name": "ACME"},
        "area": {"name": "Москва"},
        "alternate_url": "https://hh.ru/vacancy/1001",
        "salary": {"from": 300000, "to": 400000, "currency": "RUR", "gross": False},
        "description": "<p>Нужен <b>Python</b> и FastAPI</p>",
        "key_skills": [{"name": "Python"}, {"name": "FastAPI"}],
        "experience": {"id": "between3And6"},
        "schedule": {"id": "remote"},
        "employment": {"id": "full"},
        "published_at": "2099-01-01T00:00:00+0300",
        "archived": False,
        "has_test": False,
        "response_letter_required": False,
        "relations": [],
    }
    payload.update(overrides)
    return payload
