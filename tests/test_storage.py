from tests.conftest import vacancy_payload


def test_vacancy_dedupe(storage):
    storage.remember_vacancy(vacancy_payload(), "skipped", "стоп-слово")
    assert storage.is_known("1001")
    assert not storage.is_known("2002")


def test_pending_vacancy_is_not_known(storage):
    storage.remember_vacancy(vacancy_payload(), "pending")
    assert not storage.is_known("1001")


def test_application_recorded_once(storage):
    storage.record_application("1001", "res-1", "письмо", "sent")
    storage.record_application("1001", "res-1", "письмо", "failed", error="лимит")
    assert storage.stats()["applications_failed"] == 1
    assert not storage.has_applied("1001")  # статус failed не считается откликом


def test_daily_counters(storage):
    for _ in range(3):
        storage.bump("applications")
    assert storage.count("applications") == 3
    assert storage.count("chat_replies") == 0


def test_drafts_lifecycle(storage):
    draft_id = storage.add_draft("55", "Готов встретиться", "invitation", needs_human=True)
    assert [row["id"] for row in storage.list_drafts()] == [draft_id]

    storage.update_draft_text(draft_id, "Другой текст")
    storage.set_draft_status(draft_id, "sent")
    row = storage.get_draft(draft_id)
    assert row["text"] == "Другой текст" and row["status"] == "sent" and row["sent_at"]
    assert storage.list_drafts() == []


def test_seen_messages(storage):
    message = {"id": "m1", "created_at": "2026-09-01", "text": "привет",
               "author": {"participant_type": "employer"}}
    assert not storage.is_message_seen("m1")
    storage.mark_message_seen(message, "55")
    assert storage.is_message_seen("m1")


def test_interviews_are_unique_per_slot(storage):
    storage.add_interview("55", "2026-09-03T15:00:00+03:00", "2026-09-03T16:00:00+03:00")
    storage.add_interview("55", "2026-09-03T15:00:00+03:00", "2026-09-03T16:00:00+03:00")
    assert len(storage.list_interviews()) == 1
    assert storage.busy_intervals() == [
        ("2026-09-03T15:00:00+03:00", "2026-09-03T16:00:00+03:00")
    ]
