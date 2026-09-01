from hhbot.config import FiltersConfig
from hhbot.matching import hard_filter, prescore
from hhbot.models import Vacancy
from tests.conftest import vacancy_payload


def test_passes_good_vacancy(profile):
    vacancy = Vacancy.from_api(vacancy_payload())
    result = hard_filter(vacancy, FiltersConfig(min_salary=200000), profile)
    assert result.passed


def test_rejects_stop_word(profile):
    vacancy = Vacancy.from_api(vacancy_payload(name="Разработчик 1С"))
    result = hard_filter(vacancy, FiltersConfig(exclude_keywords=["1С"]), profile)
    assert not result.passed and "1С" in result.reason


def test_rejects_vacancy_with_test(profile):
    vacancy = Vacancy.from_api(vacancy_payload(has_test=True))
    assert not hard_filter(vacancy, FiltersConfig(skip_with_test=True), profile).passed


def test_rejects_already_responded(profile):
    vacancy = Vacancy.from_api(vacancy_payload(relations=["got_response"]))
    assert not hard_filter(vacancy, FiltersConfig(), profile).passed


def test_rejects_low_salary(profile):
    vacancy = Vacancy.from_api(
        vacancy_payload(salary={"from": 90000, "to": 100000, "currency": "RUR"})
    )
    result = hard_filter(vacancy, FiltersConfig(min_salary=200000), profile)
    assert not result.passed and "потолок" in result.reason


def test_missing_salary_allowed_when_configured(profile):
    vacancy = Vacancy.from_api(vacancy_payload(salary=None))
    assert hard_filter(vacancy, FiltersConfig(min_salary=200000), profile).passed
    strict = FiltersConfig(min_salary=200000, treat_missing_salary_as_ok=False)
    assert not hard_filter(vacancy, strict, profile).passed


def test_remote_only_profile_rejects_office(profile):
    profile.remote_only = True
    vacancy = Vacancy.from_api(vacancy_payload(schedule={"id": "fullDay"}))
    assert not hard_filter(vacancy, FiltersConfig(), profile).passed


def test_prescore_rewards_skill_overlap(profile):
    good = Vacancy.from_api(vacancy_payload())
    bad = Vacancy.from_api(
        vacancy_payload(
            name="Менеджер по продажам",
            description="Холодные звонки",
            key_skills=[],
            salary={"from": 60000, "currency": "RUR"},
        )
    )
    assert prescore(good, profile) > prescore(bad, profile)
