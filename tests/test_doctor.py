import time

import pytest

from hhbot.api import HhApiError
from hhbot.auth import TokenStore, Tokens
from hhbot.config import SearchQuery
from hhbot.doctor import check_api, check_config, check_credentials, check_token, run_checks
from tests.fakes import FakeHhClient


def status_of(checks, name: str) -> str:
    return next(c.status for c in checks if c.name == name)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_missing_credentials_are_reported(config):
    checks = check_credentials(config)
    assert status_of(checks, "HH_CLIENT_ID") == "fail"
    assert status_of(checks, "ANTHROPIC_API_KEY") == "fail"


def test_user_agent_must_contain_email(config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config.auth.client_id = "id"
    config.auth.client_secret = "secret"

    config.auth.user_agent = "hhbot/0.1"
    assert status_of(check_credentials(config), "HH_USER_AGENT") == "fail"

    config.auth.user_agent = "hhbot/0.1 (me@example.com)"
    checks = check_credentials(config)
    assert status_of(checks, "HH_USER_AGENT") == "ok"
    assert all(c.status == "ok" for c in checks)


def test_non_localhost_redirect_is_a_warning(config):
    config.auth.redirect_uri = "https://example.com/callback"
    assert status_of(check_credentials(config), "HH_REDIRECT_URI") == "warn"


def test_config_checks_report_empty_searches_and_profile(config):
    config.searches = []
    config.profile.summary = ""
    checks = check_config(config)
    assert status_of(checks, "searches") == "fail"
    assert status_of(checks, "profile") == "fail"
    assert status_of(checks, "dry_run") == "warn"


def test_config_checks_pass_when_filled(config):
    config.searches = [SearchQuery(name="python", text="python")]
    config.profile.summary = "Backend-разработчик"
    config.profile.resume_id = "res-1"
    config.dry_run = False
    checks = check_config(config)
    assert status_of(checks, "searches") == "ok"
    assert status_of(checks, "profile") == "ok"
    assert status_of(checks, "dry_run") == "ok"


def test_token_missing(config):
    assert check_token(config).status == "fail"


def test_token_valid(config, tmp_path):
    config.auth.token_file = tmp_path / "tokens.json"
    TokenStore(config.auth.token_file).save(
        Tokens(access_token="a", refresh_token="r", expires_at=time.time() + 3600)
    )
    check = check_token(config)
    assert check.status == "ok" and "ч" in check.detail


def test_expired_token_without_refresh_fails(config, tmp_path):
    config.auth.token_file = tmp_path / "tokens.json"
    TokenStore(config.auth.token_file).save(Tokens(access_token="a", expires_at=0))
    assert check_token(config).status == "fail"


class UnauthorizedClient(FakeHhClient):
    def dictionaries(self):
        return {}

    def me(self):
        raise HhApiError(403, {"errors": [{"value": "oauth_token_invalid"}]})


class WorkingClient(FakeHhClient):
    def dictionaries(self):
        return {"experience": []}

    def me(self):
        return {"first_name": "Иван", "last_name": "Иванов", "email": "a@b.c", "is_applicant": True}


class OfflineClient(FakeHhClient):
    def dictionaries(self):
        raise ConnectionError("сеть недоступна")


def test_api_check_detects_bad_token(config):
    checks = check_api(UnauthorizedClient(), config)
    assert status_of(checks, "авторизация") == "fail"


def test_api_check_reports_network_failure(config):
    checks = check_api(OfflineClient(), config)
    assert len(checks) == 1 and checks[0].status == "fail"


def test_api_check_happy_path(config):
    config.profile.resume_id = "res-1"
    checks = check_api(WorkingClient(), config)
    assert all(c.status == "ok" for c in checks)
    assert "res-1" in next(c.detail for c in checks if c.name == "резюме")


def test_api_check_flags_unknown_resume_id(config):
    config.profile.resume_id = "нет-такого"
    checks = check_api(WorkingClient(), config)
    assert status_of(checks, "profile.resume_id") == "fail"


def test_run_checks_without_client_marks_api_skipped(config):
    checks = run_checks(config, client=None)
    assert status_of(checks, "api.hh.ru") == "warn"


def test_placeholder_email_in_user_agent_fails(config):
    config.auth.user_agent = "hhbot/0.1 (unknown@example.com)"
    assert status_of(check_credentials(config), "HH_USER_AGENT") == "fail"
