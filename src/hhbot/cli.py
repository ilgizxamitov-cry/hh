"""CLI бота: hhbot <команда>."""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .api import HhClient
from .auth import AuthError, HhAuth, TokenStore
from .config import DEFAULT_CONFIG_PATH, BotConfig
from .llm import LLM
from .matching import hard_filter, prescore
from .models import Vacancy
from .runner import Runner
from .scheduling import Scheduler, build_calendar, interview_to_ics
from .storage import Storage

console = Console()

app = typer.Typer(add_completion=False, help="Бот для hh.ru: отклики, письма, переписка, собеседования")
auth_app = typer.Typer(help="Авторизация на hh.ru")
drafts_app = typer.Typer(help="Черновики ответов в чате")
interviews_app = typer.Typer(help="Назначенные собеседования")
app.add_typer(auth_app, name="auth")
app.add_typer(drafts_app, name="drafts")
app.add_typer(interviews_app, name="interviews")


class Context:
    def __init__(self, config_path: Path, live: bool = False, verbose: bool = False) -> None:
        load_dotenv()
        logging.basicConfig(
            level=logging.DEBUG if verbose else logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.config = BotConfig.load(config_path)
        if live:
            self.config.dry_run = False
        self.config.ensure_dirs()
        self.storage = Storage(self.config.db_path)

    @property
    def auth(self) -> HhAuth:
        cfg = self.config.auth
        if not cfg.client_id or not cfg.client_secret:
            raise typer.BadParameter(
                "Не заданы HH_CLIENT_ID / HH_CLIENT_SECRET (см. .env.example)"
            )
        return HhAuth(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            redirect_uri=cfg.redirect_uri,
            store=TokenStore(cfg.token_file),
            user_agent=cfg.user_agent,
        )

    @property
    def client(self) -> HhClient:
        auth = self.auth
        return HhClient(token_provider=auth.access_token, user_agent=self.config.auth.user_agent)

    @property
    def llm(self) -> LLM:
        return LLM(self.config.llm, self.config.profile)

    def runner(self) -> Runner:
        return Runner(self.config, self.client, self.llm, self.storage)


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Path, typer.Option("--config", "-c", help="путь к config.yaml")] = DEFAULT_CONFIG_PATH,
    live: Annotated[bool, typer.Option("--live", help="реально отправлять отклики и сообщения")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    ctx.obj = Context(config, live=live, verbose=verbose)
    if live:
        console.print("[bold red]LIVE-режим: действия будут отправлены на hh.ru[/bold red]")


def _confirm_live(ctx_obj: Context, yes: bool, what: str) -> bool:
    if ctx_obj.config.dry_run or yes:
        return True
    return typer.confirm(f"Подтвердите: {what} будут отправлены на hh.ru от вашего имени. Продолжить?")


# ---------------- init ----------------

def _template(name: str) -> str:
    return resources.files("hhbot").joinpath(name).read_text(encoding="utf-8")


@app.command()
def init(ctx: typer.Context) -> None:
    """Создать config.yaml и .env из шаблонов."""
    for target, template, hint in (
        (Path("config.yaml"), "config.example.yaml", "заполните профиль и поисковые запросы"),
        (Path(".env"), "env.example", "впишите ключи hh.ru и Anthropic"),
    ):
        if target.exists():
            console.print(f"[yellow]{target} уже существует — не трогаю[/yellow]")
            continue
        target.write_text(_template(template), encoding="utf-8")
        console.print(f"[green]Создан {target}[/green] — {hint}")


# ---------------- auth ----------------


@auth_app.command("login")
def auth_login(ctx: typer.Context, no_browser: bool = False) -> None:
    """Пройти OAuth и сохранить токены."""
    obj: Context = ctx.obj
    tokens = obj.auth.login_interactive(open_browser=not no_browser)
    console.print(f"[green]Токен получен[/green], действует до {tokens.expires_at:.0f} (unix)")


@auth_app.command("code")
def auth_code(ctx: typer.Context, code: str) -> None:
    """Обменять код авторизации на токен (если ловили редирект вручную)."""
    obj: Context = ctx.obj
    obj.auth.exchange_code(code)
    console.print("[green]Токен сохранён[/green]")


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """Проверить токен и показать владельца аккаунта."""
    obj: Context = ctx.obj
    try:
        me = obj.client.me()
    except AuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]OK[/green] {me.get('first_name', '')} {me.get('last_name', '')} "
        f"({me.get('email', '—')}), соискатель: {me.get('is_applicant')}"
    )


@app.command()
def resumes(ctx: typer.Context) -> None:
    """Список резюме на аккаунте (нужен resume_id для откликов)."""
    obj: Context = ctx.obj
    table = Table("id", "название", "обновлено", "статус")
    for resume in obj.client.my_resumes():
        table.add_row(
            str(resume.get("id")),
            resume.get("title") or "—",
            (resume.get("updated_at") or "")[:10],
            (resume.get("status") or {}).get("name", "—"),
        )
    console.print(table)


# ---------------- поиск и отклики ----------------


@app.command()
def search(
    ctx: typer.Context,
    limit: int = 20,
    show_skipped: bool = typer.Option(False, help="показывать отфильтрованные вакансии"),
) -> None:
    """Показать, что находит бот, без обращения к модели и без откликов."""
    obj: Context = ctx.obj
    client = obj.client
    table = Table("оценка", "вакансия", "компания", "зарплата", "статус")
    shown = 0
    for query in obj.config.searches:
        for item in client.search_vacancies(query.to_params(), query.max_pages):
            if shown >= limit:
                break
            full = client.get_vacancy(str(item["id"]))
            vacancy = Vacancy.from_api(full)
            check = hard_filter(vacancy, obj.config.filters, obj.config.profile)
            if not check.passed and not show_skipped:
                continue
            table.add_row(
                str(prescore(vacancy, obj.config.profile)),
                vacancy.name[:45],
                vacancy.employer[:25],
                vacancy.salary_text,
                "[green]подходит[/green]" if check.passed else f"[dim]{check.reason}[/dim]",
            )
            shown += 1
    console.print(table)


@app.command()
def apply(
    ctx: typer.Context,
    limit: Optional[int] = typer.Option(None, help="сколько откликов максимум за прогон"),
    yes: bool = typer.Option(False, "--yes", "-y", help="не спрашивать подтверждение"),
) -> None:
    """Найти вакансии, написать письма и откликнуться."""
    obj: Context = ctx.obj
    if not _confirm_live(obj, yes, "отклики с сопроводительными письмами"):
        raise typer.Abort()
    outcomes = obj.runner().run_applications(limit=limit)

    table = Table("статус", "оценка", "вакансия", "компания", "комментарий")
    for outcome in outcomes:
        colour = {"applied": "green", "dry_run": "cyan", "failed": "red"}.get(outcome.status, "dim")
        table.add_row(
            f"[{colour}]{outcome.status}[/{colour}]",
            str(outcome.fit_score or "—"),
            outcome.title[:40],
            outcome.employer[:22],
            (outcome.reason or "")[:60],
        )
    console.print(table)
    applied = [o for o in outcomes if o.status in {"applied", "dry_run"}]
    console.print(f"Обработано: {len(outcomes)}, откликов: {len(applied)}")
    if applied and obj.config.dry_run:
        console.print("[yellow]Это был dry_run. Для реальной отправки: hhbot --live apply[/yellow]")
        console.print(f"\n[bold]Пример письма ({applied[0].title}):[/bold]\n{applied[0].letter}")


# ---------------- переписка ----------------


@app.command()
def chat(
    ctx: typer.Context,
    limit: Optional[int] = typer.Option(None, help="сколько переписок обработать"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Разобрать новые сообщения работодателей и подготовить/отправить ответы."""
    obj: Context = ctx.obj
    if not _confirm_live(obj, yes, "ответы в чатах hh.ru"):
        raise typer.Abort()
    outcomes = obj.runner().run_chat(limit=limit)

    table = Table("действие", "тема", "вакансия", "компания", "комментарий")
    for outcome in outcomes:
        colour = {"sent": "green", "draft": "yellow", "dry_run": "cyan", "error": "red"}.get(
            outcome.action, "dim"
        )
        table.add_row(
            f"[{colour}]{outcome.action}[/{colour}]",
            outcome.intent or "—",
            outcome.vacancy[:35],
            outcome.employer[:20],
            (outcome.reason or "")[:60],
        )
    console.print(table)
    pending = [o for o in outcomes if o.draft_id and o.action != "sent"]
    if pending:
        console.print(
            f"[yellow]{len(pending)} ответ(ов) ждут вашего решения:[/yellow] hhbot drafts list"
        )


@drafts_app.command("list")
def drafts_list(ctx: typer.Context, status: str = "pending") -> None:
    """Черновики ответов."""
    obj: Context = ctx.obj
    table = Table("id", "тема", "компания", "вакансия", "нужен человек", "причина")
    for row in obj.storage.list_drafts(status=None if status == "all" else status):
        table.add_row(
            str(row["id"]),
            row["intent"] or "—",
            (row["employer"] or "")[:20],
            (row["vacancy_title"] or "")[:30],
            "да" if row["needs_human"] else "нет",
            (row["note"] or "")[:40],
        )
    console.print(table)


@drafts_app.command("show")
def drafts_show(ctx: typer.Context, draft_id: int) -> None:
    """Полный текст черновика."""
    obj: Context = ctx.obj
    row = obj.storage.get_draft(draft_id)
    if row is None:
        console.print("[red]не найден[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]{row['employer']} — {row['vacancy_title']}[/bold]")
    console.print(f"тема: {row['intent']}, статус: {row['status']}, причина: {row['note'] or '—'}\n")
    console.print(row["text"])


@drafts_app.command("edit")
def drafts_edit(ctx: typer.Context, draft_id: int, text: str) -> None:
    """Заменить текст черновика своим."""
    obj: Context = ctx.obj
    obj.storage.update_draft_text(draft_id, text)
    console.print("[green]обновлено[/green]")


@drafts_app.command("send")
def drafts_send(ctx: typer.Context, draft_id: int, yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Отправить черновик работодателю."""
    obj: Context = ctx.obj
    if not _confirm_live(obj, yes, "сообщение работодателю"):
        raise typer.Abort()
    from .chat import ChatAgent

    agent = ChatAgent(obj.config, obj.client, obj.llm, obj.storage)
    outcome = agent.send_draft(draft_id)
    console.print(f"[green]{outcome.action}[/green] {outcome.reason}")


@drafts_app.command("reject")
def drafts_reject(ctx: typer.Context, draft_id: int) -> None:
    """Отклонить черновик (ответите сами)."""
    obj: Context = ctx.obj
    obj.storage.set_draft_status(draft_id, "rejected")
    console.print("[green]отклонён[/green]")


# ---------------- собеседования ----------------


@interviews_app.command("list")
def interviews_list(ctx: typer.Context) -> None:
    """Назначенные и предложенные собеседования."""
    obj: Context = ctx.obj
    table = Table("id", "когда", "компания", "вакансия", "формат", "статус", "контакт")
    for row in obj.storage.list_interviews():
        table.add_row(
            str(row["id"]),
            row["starts_at"][:16].replace("T", " "),
            (row["employer"] or "")[:20],
            (row["vacancy_title"] or "")[:28],
            row["format"] or "—",
            row["status"],
            (row["contact"] or row["location"] or "—")[:25],
        )
    console.print(table)


@interviews_app.command("slots")
def interviews_slots(ctx: typer.Context, count: int = 5) -> None:
    """Ближайшие свободные слоты по вашему календарю доступности."""
    obj: Context = ctx.obj
    scheduler = Scheduler(obj.config.availability, busy=obj.storage.busy_intervals())
    for slot in scheduler.free_slots(limit=count):
        console.print(f"• {slot.human()}")


@interviews_app.command("ics")
def interviews_ics(ctx: typer.Context, output: Path = Path("interviews.ics")) -> None:
    """Выгрузить встречи в .ics для импорта в календарь."""
    obj: Context = ctx.obj
    events = [
        interview_to_ics(
            row["starts_at"],
            row["ends_at"],
            f"Собеседование: {row['employer']} — {row['vacancy_title']}",
            description=f"Контакт: {row['contact'] or '—'}\nПереписка: "
            f"https://hh.ru/negotiations?negotiationId={row['negotiation_id']}",
            location=row["location"] or row["format"] or "",
        )
        for row in obj.storage.list_interviews()
        if row["status"] != "cancelled"
    ]
    output.write_text(build_calendar(events), encoding="utf-8")
    console.print(f"[green]{len(events)} событий записано в {output}[/green]")


# ---------------- прогон и статистика ----------------


@app.command()
def run(
    ctx: typer.Context,
    loop: bool = typer.Option(False, help="крутиться в цикле с интервалом из конфига"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Полный цикл: сначала ответы в чатах, затем новые отклики."""
    import time

    obj: Context = ctx.obj
    if not _confirm_live(obj, yes, "отклики и ответы в чатах"):
        raise typer.Abort()
    runner = obj.runner()
    while True:
        report = runner.run_all()
        console.print(
            f"[bold]Итог:[/bold] откликов {len(report.applied)}, "
            f"переписок обработано {len(report.chats)}"
        )
        if not loop:
            break
        minutes = obj.config.chat.poll_interval_minutes
        console.print(f"[dim]следующий прогон через {minutes} мин[/dim]")
        time.sleep(minutes * 60)


@app.command()
def stats(ctx: typer.Context) -> None:
    """Сводка по базе бота."""
    obj: Context = ctx.obj
    table = Table("показатель", "значение")
    for key, value in obj.storage.stats().items():
        table.add_row(key, str(value))
    console.print(table)


if __name__ == "__main__":
    app()
