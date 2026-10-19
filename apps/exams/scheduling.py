"""Local Amootech policy; does not change or revoke Quiz24 hosted links."""
from django.utils import timezone

SCHEDULE_FIELDS = ("scheduled_start_at", "participation_deadline_at", "result_release_at")


def results_released(exam, now=None):
    now = now or timezone.now()
    return exam.result_release_at is None or now >= exam.result_release_at


def availability(exam, now=None):
    now = now or timezone.now()
    if exam.trashed_at:
        code, label = "TRASHED", "در سطل زباله"
    elif exam.archived_at:
        code, label = "ARCHIVED", "بایگانی‌شده"
    elif exam.status == exam.Status.CLOSED:
        code, label = "CLOSED", "پایان یافته"
    elif exam.status != exam.Status.PUBLISHED:
        code, label = "UNAVAILABLE", "آماده شرکت نیست"
    elif exam.participation_deadline_at and now >= exam.participation_deadline_at:
        code, label = "DEADLINE_PASSED", "مهلت پایان یافته"
    elif exam.scheduled_start_at and now < exam.scheduled_start_at:
        code, label = "UPCOMING", "شروع در آینده"
    else:
        code, label = "AVAILABLE", "آماده شرکت"
    return {"code": code, "label": label, "can_start": code == "AVAILABLE", "results_released": results_released(exam, now), "server_time": now.isoformat()}
