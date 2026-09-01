import pytest

from hhbot.api import HhApiError
from hhbot.config import SearchQuery
from hhbot.llm import VacancyScreen
from hhbot.runner import Runner
from tests.conftest import vacancy_payload
from tests.fakes import FakeHhClient, FakeLLM


def build(config, storage, vacancies, llm=None, client=None):
    config.searches = [SearchQuery(name="test", text="python")]
    client = client or FakeHhClient(vacancies=vacancies)
    llm = llm or FakeLLM()
    return Runner(config, client, llm, storage), client, llm


def test_dry_run_does_not_send(config, storage):
    runner, client, _ = build(config, storage, [vacancy_payload()])

    outcomes = runner.run_applications()

    assert [o.status for o in outcomes] == ["dry_run"]
    assert client.applications == []
    assert storage.has_applied("1001")
    assert outcomes[0].letter


def test_live_run_sends_application(config, storage):
    config.dry_run = False
    runner, client, _ = build(config, storage, [vacancy_payload()])

    outcomes = runner.run_applications()

    assert [o.status for o in outcomes] == ["applied"]
    assert client.applications[0][0] == "1001"
    assert client.applications[0][2]  # письмо приложено
    assert storage.count("applications") == 1


def test_hard_filter_skips_before_llm(config, storage):
    config.filters.exclude_keywords = ["1С"]
    runner, client, llm = build(config, storage, [vacancy_payload(name="Разработчик 1С")])

    outcomes = runner.run_applications()

    assert outcomes[0].status == "skipped" and "1С" in outcomes[0].reason
    assert llm.calls == []


def test_low_model_score_skips(config, storage):
    llm = FakeLLM(
        screen=VacancyScreen(
            fit_score=40, apply=False, reasons=["нужен Go, а не Python"],
            matched_skills=[], gaps=["Go"], hooks=[], red_flags=[],
        )
    )
    runner, client, _ = build(config, storage, [vacancy_payload()], llm=llm)

    outcomes = runner.run_applications()

    assert outcomes[0].status == "skipped" and "40" in outcomes[0].reason
    assert client.applications == []


def test_known_vacancy_is_not_processed_twice(config, storage):
    runner, _, llm = build(config, storage, [vacancy_payload()])
    runner.run_applications()
    calls_after_first = len(llm.calls)

    assert runner.run_applications() == []
    assert len(llm.calls) == calls_after_first


def test_run_limit_is_respected(config, storage):
    vacancies = [vacancy_payload(id=str(1000 + i)) for i in range(5)]
    runner, _, _ = build(config, storage, vacancies)

    outcomes = runner.run_applications(limit=2)

    assert len([o for o in outcomes if o.status == "dry_run"]) == 2


def test_daily_limit_stops_run(config, storage):
    config.limits.max_applications_per_day = 1
    storage.bump("applications")
    runner, _, _ = build(config, storage, [vacancy_payload()])

    assert runner.run_applications() == []


def test_fatal_hh_error_aborts_run(config, storage):
    config.dry_run = False

    class LimitedClient(FakeHhClient):
        def apply(self, vacancy_id, resume_id, message=None):
            raise HhApiError(403, {"errors": [{"value": "limit_exceeded"}]})

    client = LimitedClient(vacancies=[vacancy_payload(id="1"), vacancy_payload(id="2")])
    runner, _, _ = build(config, storage, [], client=client)

    outcomes = runner.run_applications()

    assert len(outcomes) == 1 and outcomes[0].status == "failed"
    assert "limit_exceeded" in outcomes[0].reason


def test_non_fatal_hh_error_continues(config, storage):
    config.dry_run = False

    class ConflictClient(FakeHhClient):
        def apply(self, vacancy_id, resume_id, message=None):
            raise HhApiError(403, {"errors": [{"value": "already_applied"}]})

    client = ConflictClient(vacancies=[vacancy_payload(id="1"), vacancy_payload(id="2")])
    runner, _, _ = build(config, storage, [], client=client)

    outcomes = runner.run_applications()

    assert len(outcomes) == 2
    assert all(o.status == "failed" for o in outcomes)


def test_resume_id_from_config_wins(config, storage):
    config.profile.resume_id = "my-resume"
    runner, _, _ = build(config, storage, [vacancy_payload()])
    assert runner.resolve_resume_id() == "my-resume"


def test_resume_id_falls_back_to_first(config, storage):
    runner, _, _ = build(config, storage, [vacancy_payload()])
    assert runner.resolve_resume_id() == "res-1"


def test_no_resume_raises(config, storage):
    client = FakeHhClient(vacancies=[], resumes=[])
    runner, _, _ = build(config, storage, [], client=client)
    with pytest.raises(RuntimeError):
        runner.resolve_resume_id()
