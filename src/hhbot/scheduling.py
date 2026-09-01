"""Расписание: свободные слоты под собеседования и экспорт в календарь (.ics)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import AvailabilityConfig

RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
RU_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start < other_end and other_start < self.end

    def human(self) -> str:
        """«вторник, 3 июня, 14:00-15:00 (Europe/Moscow)»"""
        tz = self.start.tzinfo
        tz_name = getattr(tz, "key", str(tz))
        return (
            f"{RU_WEEKDAYS[self.start.weekday()]}, {self.start.day} {RU_MONTHS[self.start.month - 1]}, "
            f"{self.start:%H:%M}–{self.end:%H:%M} ({tz_name})"
        )

    def to_iso(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()


class Scheduler:
    def __init__(self, config: AvailabilityConfig, busy: list[tuple[str, str]] | None = None) -> None:
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.busy: list[tuple[datetime, datetime]] = []
        for start, end in busy or []:
            try:
                self.busy.append(
                    (datetime.fromisoformat(start).astimezone(self.tz),
                     datetime.fromisoformat(end).astimezone(self.tz))
                )
            except ValueError:
                continue

    # ---------- генерация слотов ----------

    def _day_slots(self, day: date) -> list[Slot]:
        windows = self.config.weekly.get(day.weekday(), [])
        step = timedelta(minutes=self.config.slot_minutes)
        slots: list[Slot] = []
        for window in windows:
            cursor = datetime.combine(day, window.start, tzinfo=self.tz)
            window_end = datetime.combine(day, window.end, tzinfo=self.tz)
            while cursor + step <= window_end:
                slots.append(Slot(cursor, cursor + step))
                cursor += step
        return slots

    def _is_free(self, slot: Slot, now: datetime) -> bool:
        if slot.start.date() in self.config.blackout_dates:
            return False
        if slot.start < now + timedelta(hours=self.config.min_notice_hours):
            return False
        buffer = timedelta(minutes=self.config.buffer_minutes)
        for busy_start, busy_end in self.busy:
            if slot.overlaps(busy_start - buffer, busy_end + buffer):
                return False
        return True

    def free_slots(self, limit: int = 3, now: datetime | None = None) -> list[Slot]:
        now = (now or datetime.now(timezone.utc)).astimezone(self.tz)
        out: list[Slot] = []
        per_day: dict[date, int] = {}
        for offset in range(self.config.horizon_days + 1):
            day = (now + timedelta(days=offset)).date()
            for slot in self._day_slots(day):
                if len(out) >= limit:
                    return out
                if per_day.get(day, 0) >= self.config.max_per_day:
                    break
                if self._is_free(slot, now):
                    out.append(slot)
                    per_day[day] = per_day.get(day, 0) + 1
        return out

    # ---------- проверка предложения работодателя ----------

    def can_accept(self, start: datetime, duration_minutes: int | None = None,
                   now: datetime | None = None) -> tuple[bool, str]:
        """Можно ли согласиться на конкретное время. Возвращает (да/нет, причина)."""
        now = (now or datetime.now(timezone.utc)).astimezone(self.tz)
        start = start.astimezone(self.tz)
        end = start + timedelta(minutes=duration_minutes or self.config.slot_minutes)

        if start.date() in self.config.blackout_dates:
            return False, "этот день помечен как недоступный"
        if start < now:
            return False, "время уже прошло"
        if start < now + timedelta(hours=self.config.min_notice_hours):
            return False, f"меньше {self.config.min_notice_hours} ч до встречи"

        windows = self.config.weekly.get(start.weekday(), [])
        if not windows:
            return False, "в этот день недели встречи не назначаются"
        inside = any(
            datetime.combine(start.date(), w.start, tzinfo=self.tz) <= start
            and end <= datetime.combine(start.date(), w.end, tzinfo=self.tz)
            for w in windows
        )
        if not inside:
            return False, "время вне окна доступности"

        buffer = timedelta(minutes=self.config.buffer_minutes)
        for busy_start, busy_end in self.busy:
            if start < busy_end + buffer and busy_start - buffer < end:
                return False, "пересекается с уже назначенной встречей"

        return True, "время свободно"


# ---------- экспорт в календарь ----------

def _ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")
    )


def _ics_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def interview_to_ics(
    starts_at: str,
    ends_at: str,
    summary: str,
    description: str = "",
    location: str = "",
    uid: str | None = None,
) -> str:
    start = datetime.fromisoformat(starts_at)
    end = datetime.fromisoformat(ends_at)
    uid = uid or hashlib.sha1(f"{starts_at}{summary}".encode()).hexdigest() + "@hhbot"
    return "\r\n".join(
        [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{_ics_dt(datetime.now(timezone.utc))}",
            f"DTSTART:{_ics_dt(start)}",
            f"DTEND:{_ics_dt(end)}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(description)}",
            f"LOCATION:{_ics_escape(location)}",
            "BEGIN:VALARM",
            "TRIGGER:-PT60M",
            "ACTION:DISPLAY",
            "DESCRIPTION:Собеседование через час",
            "END:VALARM",
            "END:VEVENT",
        ]
    )


def build_calendar(events: list[str]) -> str:
    return "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//hhbot//RU", "CALSCALE:GREGORIAN",
         *events, "END:VCALENDAR", ""]
    )
