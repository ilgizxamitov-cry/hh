from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hhbot.scheduling import Scheduler, build_calendar, interview_to_ics

MSK = ZoneInfo("Europe/Moscow")


def test_free_slots_respect_notice_and_max_per_day(availability):
    scheduler = Scheduler(availability)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=MSK)  # вторник
    slots = scheduler.free_slots(limit=5, now=now)

    assert slots
    assert all(slot.start >= now + timedelta(hours=availability.min_notice_hours) for slot in slots)
    per_day: dict = {}
    for slot in slots:
        per_day[slot.start.date()] = per_day.get(slot.start.date(), 0) + 1
    assert max(per_day.values()) <= availability.max_per_day


def test_busy_interval_blocks_slot_with_buffer(availability):
    busy = [("2026-09-03T12:00:00+03:00", "2026-09-03T13:00:00+03:00")]
    scheduler = Scheduler(availability, busy=busy)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=MSK)

    ok, reason = scheduler.can_accept(datetime(2026, 9, 3, 12, 30, tzinfo=MSK), now=now)
    assert not ok and "пересекается" in reason

    ok, _ = scheduler.can_accept(datetime(2026, 9, 3, 15, 0, tzinfo=MSK), now=now)
    assert ok


def test_rejects_outside_window_and_weekend(availability):
    scheduler = Scheduler(availability)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=MSK)

    ok, reason = scheduler.can_accept(datetime(2026, 9, 3, 22, 0, tzinfo=MSK), now=now)
    assert not ok and "вне окна" in reason

    ok, reason = scheduler.can_accept(datetime(2026, 9, 5, 12, 0, tzinfo=MSK), now=now)  # суббота
    assert not ok


def test_rejects_too_soon(availability):
    scheduler = Scheduler(availability)
    now = datetime(2026, 9, 2, 11, 0, tzinfo=MSK)
    ok, reason = scheduler.can_accept(datetime(2026, 9, 2, 15, 0, tzinfo=MSK), now=now)
    assert not ok and "меньше" in reason


def test_blackout_date(availability):
    availability.blackout_dates = [datetime(2026, 9, 3).date()]
    scheduler = Scheduler(availability)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=MSK)
    ok, reason = scheduler.can_accept(datetime(2026, 9, 3, 12, 0, tzinfo=MSK), now=now)
    assert not ok and "недоступн" in reason


def test_slot_human_readable(availability):
    scheduler = Scheduler(availability)
    slots = scheduler.free_slots(limit=1, now=datetime(2026, 9, 1, 9, 0, tzinfo=MSK))
    text = slots[0].human()
    assert "Europe/Moscow" in text and "–" in text


def test_ics_export_is_wellformed():
    event = interview_to_ics(
        "2026-09-03T15:00:00+03:00", "2026-09-03T16:00:00+03:00", "Собеседование: ACME"
    )
    calendar = build_calendar([event])
    assert calendar.startswith("BEGIN:VCALENDAR")
    assert "DTSTART:20260903T120000Z" in calendar  # 15:00 MSK == 12:00 UTC
    assert calendar.rstrip().endswith("END:VCALENDAR")
