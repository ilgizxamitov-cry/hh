"""Локальное состояние бота (SQLite): что видели, куда откликнулись, что написали."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    employer     TEXT,
    area         TEXT,
    url          TEXT,
    salary_from  INTEGER,
    salary_to    INTEGER,
    currency     TEXT,
    published_at TEXT,
    seen_at      TEXT NOT NULL,
    decision     TEXT,              -- applied|skipped|pending|error
    reason       TEXT,
    fit_score    INTEGER,
    raw          TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    vacancy_id     TEXT PRIMARY KEY,
    negotiation_id TEXT,
    resume_id      TEXT,
    cover_letter   TEXT,
    applied_at     TEXT NOT NULL,
    status         TEXT NOT NULL,   -- sent|dry_run|failed
    error          TEXT
);

CREATE TABLE IF NOT EXISTS seen_messages (
    message_id     TEXT PRIMARY KEY,
    negotiation_id TEXT NOT NULL,
    author         TEXT,
    created_at     TEXT,
    text           TEXT,
    processed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    negotiation_id TEXT NOT NULL,
    vacancy_title  TEXT,
    employer       TEXT,
    reply_to       TEXT,
    intent         TEXT,
    text           TEXT NOT NULL,
    status         TEXT NOT NULL,   -- pending|sent|rejected|dry_run
    needs_human    INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    meta           TEXT,
    created_at     TEXT NOT NULL,
    sent_at        TEXT
);

CREATE TABLE IF NOT EXISTS interviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    negotiation_id TEXT NOT NULL,
    vacancy_title  TEXT,
    employer       TEXT,
    starts_at      TEXT NOT NULL,   -- ISO 8601 с таймзоной
    ends_at        TEXT NOT NULL,
    format         TEXT,
    location       TEXT,
    contact        TEXT,
    status         TEXT NOT NULL,   -- proposed|confirmed|cancelled
    created_at     TEXT NOT NULL,
    UNIQUE(negotiation_id, starts_at)
);

CREATE TABLE IF NOT EXISTS counters (
    day   TEXT NOT NULL,
    key   TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_seen_neg ON seen_messages(negotiation_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- вакансии ----------

    def is_known(self, vacancy_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM vacancies WHERE id = ? AND decision IS NOT NULL AND decision != 'pending'",
            (vacancy_id,),
        ).fetchone()
        return row is not None

    def remember_vacancy(
        self,
        vacancy: dict[str, Any],
        decision: str | None = None,
        reason: str | None = None,
        fit_score: int | None = None,
    ) -> None:
        salary = vacancy.get("salary") or {}
        self.conn.execute(
            """
            INSERT INTO vacancies (id, name, employer, area, url, salary_from, salary_to,
                                   currency, published_at, seen_at, decision, reason, fit_score, raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                decision = COALESCE(excluded.decision, vacancies.decision),
                reason = COALESCE(excluded.reason, vacancies.reason),
                fit_score = COALESCE(excluded.fit_score, vacancies.fit_score)
            """,
            (
                str(vacancy.get("id")),
                vacancy.get("name"),
                (vacancy.get("employer") or {}).get("name"),
                (vacancy.get("area") or {}).get("name"),
                vacancy.get("alternate_url"),
                salary.get("from"),
                salary.get("to"),
                salary.get("currency"),
                vacancy.get("published_at"),
                _now(),
                decision,
                reason,
                fit_score,
                json.dumps(vacancy, ensure_ascii=False)[:200_000],
            ),
        )
        self.conn.commit()

    # ---------- отклики ----------

    def record_application(
        self,
        vacancy_id: str,
        resume_id: str | None,
        cover_letter: str,
        status: str,
        negotiation_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO applications (vacancy_id, negotiation_id, resume_id, cover_letter,
                                      applied_at, status, error)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(vacancy_id) DO UPDATE SET
                status = excluded.status, error = excluded.error,
                negotiation_id = COALESCE(excluded.negotiation_id, applications.negotiation_id)
            """,
            (vacancy_id, negotiation_id, resume_id, cover_letter, _now(), status, error),
        )
        self.conn.commit()

    def has_applied(self, vacancy_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM applications WHERE vacancy_id = ? AND status IN ('sent','dry_run')",
            (vacancy_id,),
        ).fetchone()
        return row is not None

    # ---------- переписка ----------

    def is_message_seen(self, message_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM seen_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            is not None
        )

    def mark_message_seen(self, message: dict[str, Any], negotiation_id: str) -> None:
        author = (message.get("author") or {}).get("participant_type")
        self.conn.execute(
            """INSERT OR IGNORE INTO seen_messages
               (message_id, negotiation_id, author, created_at, text, processed_at)
               VALUES (?,?,?,?,?,?)""",
            (
                str(message.get("id")),
                negotiation_id,
                author,
                message.get("created_at"),
                (message.get("text") or "")[:20_000],
                _now(),
            ),
        )
        self.conn.commit()

    def add_draft(
        self,
        negotiation_id: str,
        text: str,
        intent: str,
        status: str = "pending",
        needs_human: bool = False,
        note: str = "",
        vacancy_title: str = "",
        employer: str = "",
        reply_to: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO drafts (negotiation_id, vacancy_title, employer, reply_to, intent,
                                   text, status, needs_human, note, meta, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                negotiation_id,
                vacancy_title,
                employer,
                reply_to,
                intent,
                text,
                status,
                int(needs_human),
                note,
                json.dumps(meta or {}, ensure_ascii=False),
                _now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_drafts(self, status: str | None = "pending") -> list[sqlite3.Row]:
        if status:
            return list(
                self.conn.execute(
                    "SELECT * FROM drafts WHERE status = ? ORDER BY id DESC", (status,)
                )
            )
        return list(self.conn.execute("SELECT * FROM drafts ORDER BY id DESC"))

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()

    def set_draft_status(self, draft_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE drafts SET status = ?, sent_at = ? WHERE id = ?",
            (status, _now() if status == "sent" else None, draft_id),
        )
        self.conn.commit()

    def update_draft_text(self, draft_id: int, text: str) -> None:
        self.conn.execute("UPDATE drafts SET text = ? WHERE id = ?", (text, draft_id))
        self.conn.commit()

    # ---------- собеседования ----------

    def add_interview(
        self,
        negotiation_id: str,
        starts_at: str,
        ends_at: str,
        status: str = "proposed",
        vacancy_title: str = "",
        employer: str = "",
        fmt: str = "online",
        location: str = "",
        contact: str = "",
    ) -> int:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO interviews
               (negotiation_id, vacancy_title, employer, starts_at, ends_at, format,
                location, contact, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                negotiation_id,
                vacancy_title,
                employer,
                starts_at,
                ends_at,
                fmt,
                location,
                contact,
                status,
                _now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_interviews(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return list(
                self.conn.execute(
                    "SELECT * FROM interviews WHERE status = ? ORDER BY starts_at", (status,)
                )
            )
        return list(self.conn.execute("SELECT * FROM interviews ORDER BY starts_at"))

    def busy_intervals(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT starts_at, ends_at FROM interviews WHERE status IN ('proposed','confirmed')"
        )
        return [(r["starts_at"], r["ends_at"]) for r in rows]

    def set_interview_status(self, interview_id: int, status: str) -> None:
        self.conn.execute("UPDATE interviews SET status = ? WHERE id = ?", (status, interview_id))
        self.conn.commit()

    # ---------- счётчики дневных лимитов ----------

    def bump(self, key: str, amount: int = 1, day: str | None = None) -> int:
        day = day or date.today().isoformat()
        self.conn.execute(
            """INSERT INTO counters (day, key, value) VALUES (?,?,?)
               ON CONFLICT(day, key) DO UPDATE SET value = value + excluded.value""",
            (day, key, amount),
        )
        self.conn.commit()
        return self.count(key, day)

    def count(self, key: str, day: str | None = None) -> int:
        day = day or date.today().isoformat()
        row = self.conn.execute(
            "SELECT value FROM counters WHERE day = ? AND key = ?", (day, key)
        ).fetchone()
        return int(row["value"]) if row else 0

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str, args: Iterable[Any] = ()) -> int:
            return int(self.conn.execute(sql, tuple(args)).fetchone()[0])

        return {
            "vacancies_seen": scalar("SELECT COUNT(*) FROM vacancies"),
            "applications_sent": scalar("SELECT COUNT(*) FROM applications WHERE status='sent'"),
            "applications_dry_run": scalar(
                "SELECT COUNT(*) FROM applications WHERE status='dry_run'"
            ),
            "applications_failed": scalar("SELECT COUNT(*) FROM applications WHERE status='failed'"),
            "drafts_pending": scalar("SELECT COUNT(*) FROM drafts WHERE status='pending'"),
            "interviews": scalar("SELECT COUNT(*) FROM interviews WHERE status!='cancelled'"),
            "applications_today": self.count("applications"),
            "chat_replies_today": self.count("chat_replies"),
        }
