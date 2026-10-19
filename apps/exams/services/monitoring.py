"""Conservative reconciliation of the documented invited-participant listing."""
import time
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Case, CharField, Count, Exists, F, OuterRef, Q, Subquery, Value, When
from django.utils import timezone
from django.db.models.functions import Concat

from ..configuration import Quiz24Config
from ..exceptions import Quiz24Error
from ..models import Exam, ExamAssignment, ExamAttempt, ExamMonitoring, ExamParticipation
from ..scheduling import availability
from .quiz24 import Quiz24Service, quiz24_mobile

COOLDOWN = timedelta(seconds=60)
LEASE = timedelta(seconds=90)
FRESHNESS = timedelta(minutes=5)
ERRORS = {
    "PROVIDER_UNAVAILABLE": "دریافت وضعیت از سرویس آزمون ناموفق بود؛ آخرین اطلاعات حفظ شد.",
    "MALFORMED": "پاسخ سرویس کامل یا قابل تأیید نیست؛ وضعیت‌های نامطمئن تأیید نشده‌اند.",
    "PARTIAL": "اطلاعات سرویس بخشی یا دارای موارد تطبیق‌ناپذیر است؛ نبودن نام، غیبت محسوب نمی‌شود.",
    "EXAM_CHANGED": "اطلاعات آزمون تغییر کرده است؛ همگام‌سازی را دوباره انجام دهید.",
}


def identity(student, school):
    if not student.mobile_verified_at or student.quiz24_sync_status != "SYNCED" or not student.quiz24_student_id or student.quiz24_school_id != school:
        return None
    try:
        username = quiz24_mobile(student.mobile)
    except Quiz24Error:
        return None
    return username, (student.quiz24_student_id, school, student.mobile_verified_at)


def provider_username(value):
    # The docs type userName as integer, while registration uses a mobile login.
    if type(value) not in (str, int):
        return None
    value = str(value).strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if len(value) == 10 and value.startswith("9") and value.isdecimal():
        value = "0" + value
    try:
        return quiz24_mobile(value)
    except Quiz24Error:
        return None


def parse_participants(response):
    if not isinstance(response, dict) or str(response.get("status")) != "100" or not isinstance(response.get("result"), list):
        raise Quiz24Error(None, "MALFORMED")
    rows, partial = {}, False
    for row in response["result"]:
        if not isinstance(row, dict):
            partial = True
            continue
        username = provider_username(row.get("userName"))
        started, complete = row.get("isStarted"), row.get("isComplete")
        if not username or type(started) is not bool or type(complete) is not bool or (complete and not started):
            partial = True
            continue
        status = "COMPLETED" if complete else "IN_PROGRESS" if started else "NOT_STARTED"
        if username in rows and rows[username] != status:
            rows[username] = None
            partial = True
        elif username not in rows:
            rows[username] = status
    return rows, partial


def sync_monitoring(exam_id, user):
    now, token = timezone.now(), uuid.uuid4()
    # A conditional DB update is the claim, including on backends without row locks.
    with transaction.atomic():
        exam = Exam.objects.select_for_update().get(pk=exam_id, counselor__user=user, is_personal_practice=False)
        if not exam.provider_exam_code:
            return {"code": "MISSING_EXAM_CODE", "detail": "کد آزمون ثبت نشده است؛ ابتدا آزمون را بررسی و تکمیل کنید."}, 409
        if not exam.assignments.filter(student__counselor=exam.counselor).exists():
            return {"code": "NO_ASSIGNMENTS", "detail": "دانش‌آموز مجازی برای پایش تخصیص داده نشده است."}, 409
        state, _ = ExamMonitoring.objects.get_or_create(exam=exam)
        if state.lease_expires_at and state.lease_expires_at > now:
            return {"code": "SYNC_IN_PROGRESS", "detail": "به‌روزرسانی وضعیت در حال انجام است."}, 409
        if state.last_attempt_at and now - state.last_attempt_at < COOLDOWN:
            remaining = max(1, int((state.last_attempt_at + COOLDOWN - now).total_seconds()) + 1)
            return {"code": "SYNC_COOLDOWN", "detail": "برای به‌روزرسانی دوباره کمی صبر کنید.", "retry_after": remaining}, 429
        claimed = ExamMonitoring.objects.filter(pk=state.pk).filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).filter(Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=now - COOLDOWN)).update(last_attempt_at=now, lease_token=token, lease_expires_at=now + LEASE)
        if not claimed:
            return {"code": "SYNC_IN_PROGRESS", "detail": "به‌روزرسانی دیگری در حال انجام است."}, 409
        code, counselor_id = exam.provider_exam_code, exam.counselor_id
        school = Quiz24Config().user_id
        snapshots = {}
        for assignment in exam.assignments.filter(student__counselor=exam.counselor).select_related("student"):
            mapping = identity(assignment.student, school)
            if mapping:
                snapshots[assignment.pk] = (assignment.student_id, *mapping)
        if not snapshots:
            ExamMonitoring.objects.filter(pk=state.pk, lease_token=token).update(lease_token=None, lease_expires_at=None)
            return {"code": "IDENTITY_NOT_READY", "detail": "ابتدا اتصال تأییدشده دانش‌آموزان به Quiz24 را تکمیل کنید."}, 409
    # HTTP only outside transactions. Bound page count, total time, and per-call timeout.
    collected, partial, error = {}, False, ""
    seen_pages = set()
    try:
        service, began = Quiz24Service(), time.monotonic()
        for page in range(1, 21):
            remaining = 30 - (time.monotonic() - began)
            if remaining <= 0:
                raise Quiz24Error(None, "MALFORMED")
            response = service.get_exam_participants(code, page, timeout=min(15.0, remaining))
            rows, page_partial = parse_participants(response)
            if rows:
                signature = tuple(sorted(rows.items()))
                if signature in seen_pages:
                    raise Quiz24Error(None, "MALFORMED")
                seen_pages.add(signature)
            partial |= page_partial
            for username, status in rows.items():
                if username in collected and collected[username] != status:
                    collected[username] = None
                    partial = True
                elif username not in collected:
                    collected[username] = status
            if not response["result"]:
                break
        else:
            raise Quiz24Error(None, "MALFORMED")
    except Quiz24Error as exc:
        error = "MALFORMED" if exc.message == "MALFORMED" else "PROVIDER_UNAVAILABLE"
    except Exception:
        # Never persist provider bodies, names, identifiers, or exception messages.
        error = "PROVIDER_UNAVAILABLE"
    with transaction.atomic():
        exam = Exam.objects.select_for_update().get(pk=exam_id)
        state = ExamMonitoring.objects.select_for_update().get(exam_id=exam_id)
        if state.lease_token != token:
            return {"code": "SYNC_SUPERSEDED", "detail": "نتیجه درخواست قدیمی کنار گذاشته شد."}, 409
        if exam.provider_exam_code != code or exam.counselor_id != counselor_id or exam.is_personal_practice:
            error = "EXAM_CHANGED"
        if not error:
            recognized = set()
            existing = {row.assignment_id: row for row in ExamParticipation.objects.filter(assignment__exam=exam)}
            inserts, updates = [], []
            for assignment in ExamAssignment.objects.filter(exam=exam, student__counselor_id=counselor_id).select_related("student"):
                mapping = identity(assignment.student, school)
                snapshot = snapshots.get(assignment.pk)
                if not mapping or snapshot != (assignment.student_id, *mapping):
                    continue
                username, identity_key = mapping
                recognized.add(username)
                status = collected.get(username)
                if status is None:
                    continue
                previous = existing.get(assignment.pk)
                if previous and previous.status == "COMPLETED":
                    continue
                values = dict(status=status, observed_at=timezone.now(), identity_student_id=identity_key[0], identity_school_id=identity_key[1], identity_verified_at=identity_key[2])
                if previous:
                    for key, value in values.items():
                        setattr(previous, key, value)
                    updates.append(previous)
                else:
                    inserts.append(ExamParticipation(assignment=assignment, **values))
            ExamParticipation.objects.bulk_create(inserts)
            ExamParticipation.objects.bulk_update(updates, ("status", "observed_at", "identity_student_id", "identity_school_id", "identity_verified_at"))
            partial |= bool(set(collected) - recognized)
            state.partial = partial
            state.last_success_at = timezone.now()
            state.error = "PARTIAL" if partial else ""
        else:
            state.error, state.partial = error, True
        state.lease_token, state.lease_expires_at = None, None
        state.save()
    if error == "EXAM_CHANGED":
        return {"code": error, "detail": ERRORS[error]}, 409
    return None, 502 if error else 200


def monitoring_data(exam, query):
    now, school = timezone.now(), Quiz24Config().user_id
    state = ExamMonitoring.objects.filter(exam=exam).first()
    fresh = bool(state and state.last_success_at and (not state.last_attempt_at or state.last_success_at >= state.last_attempt_at) and now - state.last_success_at <= FRESHNESS and state.error in ("", "PARTIAL") and not (state.lease_expires_at and state.lease_expires_at > now))
    attempt = ExamAttempt.objects.filter(exam=exam, student_id=OuterRef("student_id"))
    rows = exam.assignments.filter(student__counselor=exam.counselor).select_related("student__user", "participation").annotate(
        local_completed=Exists(attempt.filter(status="COMPLETED")),
        local_status=Subquery(attempt.values("status")[:1]),
        local_requested_at=Subquery(attempt.values("created_at")[:1]),
        local_finished_at=Subquery(attempt.filter(status="COMPLETED").values("finished_at")[:1]),
    )
    # SQL status filtering/pagination; identity conditions are rechecked in each row.
    verified_identity = Q(student__mobile_verified_at__isnull=False, student__quiz24_sync_status="SYNCED", student__quiz24_school_id=school, student__quiz24_student_id__isnull=False)
    recent_verified = verified_identity & Q(participation__identity_student_id=F("student__quiz24_student_id"), participation__identity_school_id=F("student__quiz24_school_id"), participation__identity_verified_at=F("student__mobile_verified_at"), participation__observed_at__gte=max(now - FRESHNESS, state.last_attempt_at) if state and state.last_attempt_at else now - FRESHNESS, participation__status__in=("NOT_STARTED", "IN_PROGRESS"))
    rows = rows.annotate(monitor_status=Case(
        When(Q(local_completed=True) | Q(participation__status="COMPLETED"), then=Value("COMPLETED")),
        When(recent_verified if fresh else Q(pk__isnull=True), then=F("participation__status")),
        default=Value("UNKNOWN"), output_field=CharField(),
    ))
    # Nonterminal observations are bound to the verified identity at sync time.
    totals = {key: 0 for key in ("NOT_STARTED", "IN_PROGRESS", "COMPLETED", "UNKNOWN")}
    for entry in rows.values("monitor_status").annotate(total=Count("pk")):
        totals[entry["monitor_status"]] += entry["total"]
    summary = {"assigned": sum(totals.values()), "started": totals["IN_PROGRESS"] + totals["COMPLETED"], **totals}
    q = query.get("q", "").strip()[:100]
    if q:
        rows = rows.annotate(search_name=Concat("student__user__first_name", Value(" "), "student__user__last_name")).filter(Q(search_name__icontains=q) | Q(student__user__username__icontains=q))
    status = query.get("status", "")
    if status:
        rows = rows.filter(monitor_status=status)
    metadata = {"summary": summary, "last_success_at": state.last_success_at if state else None, "last_attempt_at": state.last_attempt_at if state else None, "fresh": fresh, "fresh_until": state.last_success_at + FRESHNESS if fresh else None, "partial": state.partial if state else True, "error": ERRORS.get(state.error, "") if state else "", "syncing": bool(state and state.lease_expires_at and state.lease_expires_at > now), "retry_after": max(0, int((state.last_attempt_at + COOLDOWN - now).total_seconds()) + 1) if state and state.last_attempt_at else 0, "availability": availability(exam, now)}
    return rows.order_by("pk"), metadata


def monitoring_row(row, exam):
    observation = getattr(row, "participation", None)
    mapping = identity(row.student, Quiz24Config().user_id)
    warnings = []
    if not mapping:
        warnings.append("اتصال تأییدشده Quiz24 کامل نیست.")
    if row.local_status == "PENDING":
        warnings.append("درخواست لینک در آموزتک ثبت شده؛ این به‌تنهایی شروع آزمون را ثابت نمی‌کند.")
    return {"student": row.student_id, "student_name": row.student.user.get_full_name() or row.student.user.username, "status": row.monitor_status, "last_known_status": observation.status if observation else None, "observed_at": observation.observed_at if observation else None, "started_at": None, "completed_at": None, "local_attempt": {"status": row.local_status, "requested_at": row.local_requested_at, "finished_at": row.local_finished_at} if row.local_status else None, "absent": bool(row.monitor_status == "NOT_STARTED" and exam.participation_deadline_at and observation and observation.observed_at >= exam.participation_deadline_at and timezone.now() >= exam.participation_deadline_at), "warnings": warnings}
