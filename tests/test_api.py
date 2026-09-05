import httpx
import pytest
import respx

from hhbot.api import HhApiError, HhClient
from hhbot.auth import AuthError


def make_client(**kwargs) -> HhClient:
    return HhClient(
        token_provider=lambda: "token-123",
        user_agent="hhbot-test/0.1 (test@example.com)",
        min_interval=0.0,
        **kwargs,
    )


@respx.mock
def test_sends_required_headers():
    route = respx.get("https://api.hh.ru/me").mock(return_value=httpx.Response(200, json={"id": "1"}))
    make_client().me()

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer token-123"
    assert request.headers["HH-User-Agent"].startswith("hhbot-test/")


@respx.mock
def test_search_paginates_until_last_page():
    respx.get("https://api.hh.ru/vacancies").mock(
        side_effect=[
            httpx.Response(200, json={"items": [{"id": "1"}, {"id": "2"}], "pages": 2}),
            httpx.Response(200, json={"items": [{"id": "3"}], "pages": 2}),
        ]
    )
    items = list(make_client().search_vacancies({"text": "python"}, max_pages=5))
    assert [i["id"] for i in items] == ["1", "2", "3"]


@respx.mock
def test_apply_posts_form_fields():
    route = respx.post("https://api.hh.ru/negotiations").mock(return_value=httpx.Response(201))
    make_client().apply("1001", "res-1", "Здравствуйте")

    body = route.calls[0].request.content.decode()
    assert "vacancy_id=1001" in body and "resume_id=res-1" in body and "message=" in body


@respx.mock
def test_apply_error_exposes_codes():
    respx.post("https://api.hh.ru/negotiations").mock(
        return_value=httpx.Response(
            403, json={"errors": [{"type": "negotiations", "value": "already_applied"}]}
        )
    )
    with pytest.raises(HhApiError) as exc:
        make_client().apply("1001", "res-1", "текст")
    assert "already_applied" in exc.value.codes


@respx.mock
def test_retries_on_429_then_succeeds():
    respx.get("https://api.hh.ru/me").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": "42"}),
        ]
    )
    assert make_client(max_retries=2).me()["id"] == "42"


@respx.mock
def test_gives_up_after_max_retries():
    respx.get("https://api.hh.ru/me").mock(
        return_value=httpx.Response(500, headers={"Retry-After": "0"})
    )
    with pytest.raises(HhApiError):
        make_client(max_retries=1).me()


@respx.mock
def test_send_message_uses_message_field():
    route = respx.post("https://api.hh.ru/negotiations/55/messages").mock(
        return_value=httpx.Response(201)
    )
    make_client().send_message("55", "Спасибо, подтверждаю")
    assert "message=" in route.calls[0].request.content.decode()


@respx.mock
def test_anonymous_client_can_search():
    """Поиск вакансий доступен без токена соискателя."""
    route = respx.get("https://api.hh.ru/vacancies").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "7"}], "pages": 1})
    )
    client = HhClient(token_provider=None, user_agent="hhbot-test/0.1 (t@e.com)", min_interval=0.0)

    items = list(client.search_vacancies({"text": "python"}))

    assert [i["id"] for i in items] == ["7"]
    assert "Authorization" not in route.calls[0].request.headers
    assert not client.authorized


@respx.mock
def test_anonymous_client_refuses_applicant_endpoints():
    client = HhClient(token_provider=None, user_agent="hhbot-test/0.1 (t@e.com)", min_interval=0.0)
    for call in (
        lambda: client.apply("1", "res-1", "текст"),
        lambda: client.my_resumes(),
        lambda: client.send_message("55", "привет"),
    ):
        with pytest.raises(AuthError):
            call()


@respx.mock
def test_search_uses_token_when_available():
    """С токеном в выдаче приходит relations — видно, куда уже откликались."""
    route = respx.get("https://api.hh.ru/vacancies").mock(
        return_value=httpx.Response(200, json={"items": [], "pages": 1})
    )
    list(make_client().search_vacancies({"text": "python"}))
    assert route.calls[0].request.headers["Authorization"] == "Bearer token-123"


@respx.mock
def test_expired_token_does_not_break_public_search():
    def raise_auth() -> str:
        raise AuthError("токен истёк")

    respx.get("https://api.hh.ru/vacancies").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "7"}], "pages": 1})
    )
    client = HhClient(
        token_provider=raise_auth, user_agent="hhbot-test/0.1 (t@e.com)", min_interval=0.0
    )

    assert [i["id"] for i in client.search_vacancies({"text": "python"})] == ["7"]
    with pytest.raises(AuthError):
        client.my_resumes()
