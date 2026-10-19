"""Same-exam raw Quiz24 Score comparisons, never percentage or official taz."""
import hashlib
import math
import statistics
import time
import uuid
from datetime import timedelta, timezone as datetime_timezone
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import StudentProfile
from ..configuration import Quiz24Config
from ..exceptions import Quiz24Error
from ..models import Exam, ExamAnswer, ExamAttempt, ExamAnalyticsSync, ExamAssignment
from .monitoring import identity
from .quiz24 import Quiz24Service
from .results import _fields, _number, parse_answers

COOLDOWN = timedelta(seconds=60)
LEASE = timedelta(seconds=90)
FRESHNESS = timedelta(minutes=5)
BATCH_SIZE = 10
SCORE_SOURCE = "QUIZ24_SCORE"


def result_identity(student, school=None):
    mapping = identity(student, school if school is not None else Quiz24Config().user_id)
    if not mapping:
        return None
    username, (external_id, school_id, verified_at) = mapping
    value = f"{username}:{external_id}:{school_id}:{verified_at.astimezone(datetime_timezone.utc).isoformat()}"
    return hashlib.sha256(value.encode()).hexdigest()


def strict_result(response, question_type):
    """Read only the overall documented summary; never mine Course/answer rows."""
    if not isinstance(response, dict) or str(response.get("status")) != "100":
        raise Quiz24Error(None, "نتیجه معتبر و کامل در دسترس نیست.")
    result = response.get("result")
    if isinstance(result, list) and len(result) == 1:
        result = result[0]
    if not isinstance(result, dict):
        raise Quiz24Error(None, "خلاصه نتیجه آزمون کامل نیست.")
    fields = _fields(result)
    if any(key in fields for key in ("courseid", "coursename", "order")) or isinstance(fields.get("course"), str):
        raise Quiz24Error(None, "خلاصه کل آزمون قابل تشخیص نیست.")
    metrics = {}
    for target, source in (("score", "score"), ("balance", "balance"), ("rank", "rank"), ("provider_rank_in_class", "rankinclass"), ("correct_count", "correct"), ("wrong_count", "wrong"), ("no_answer_count", "noanswer")):
        value = fields.get(source)
        # Provider docs call Score a Double total score, not a percentage.
        metrics[target] = None if isinstance(value, str) and "%" in value else _number(value, integer=target not in ("score", "balance"))
    for key in ("rank", "provider_rank_in_class"):
        if metrics[key] is not None and metrics[key] < 1:
            metrics[key] = None
    if metrics["score"] is None:
        raise Quiz24Error(None, "نمره کل قابل مقایسه موجود نیست؛ نتیجه قبلی حفظ شد.")
    answers = parse_answers(response, question_type)
    return metrics, answers


def verification_defaults(exam, student, response, metrics):
    defaults = dict(result_verified_at=None, result_identity_digest="", result_exam_code="", score_source="", provider_rank_in_class=None)
    try:
        strict, _ = strict_result(response, exam.question_type)
    except Quiz24Error:
        return defaults
    digest = result_identity(student)
    if digest:
        defaults.update(**strict)
        defaults.update(result_verified_at=timezone.now(), result_identity_digest=digest, result_exam_code=exam.provider_exam_code or "", score_source=SCORE_SOURCE, provider_rank_in_class=strict["provider_rank_in_class"])
    return defaults


def population(exam):
    assignments = list(exam.assignments.filter(student__counselor_id=exam.counselor_id).select_related("student__user", "participation").order_by("pk"))
    attempts = {row.student_id: row for row in ExamAttempt.objects.filter(exam=exam, student_id__in=[row.student_id for row in assignments]).defer("raw_result")}
    return assignments, attempts


def is_completed(assignment, attempt):
    evidence = getattr(assignment, "participation", None)
    return bool(attempt and attempt.status == "COMPLETED" or evidence and evidence.status == "COMPLETED")


def distribution(scores):
    if not scores:
        return []
    low, high = min(scores), max(scores)
    if low == high:
        return [{"lower": low, "upper": high, "count": len(scores), "upper_inclusive": True}]
    low_decimal, high_decimal = Decimal(str(low)), Decimal(str(high))
    width = (high_decimal - low_decimal) / 5
    bins = [{"lower": float(low_decimal + i * width), "upper": float(low_decimal + (i + 1) * width), "count": 0, "upper_inclusive": i == 4} for i in range(5)]
    for score in scores:
        bins[min(4, int((Decimal(str(score)) - low_decimal) / width))]["count"] += 1
    return bins


def analytics_data(exam, query):
    now = timezone.now()
    school = Quiz24Config().user_id
    assignments, attempts = population(exam)
    rows, comparable = [], []
    completed = 0
    for assignment in assignments:
        attempt = attempts.get(assignment.student_id)
        done = is_completed(assignment, attempt)
        completed += int(done)
        digest = result_identity(assignment.student, school)
        verified = bool(done and attempt and attempt.status == "COMPLETED" and attempt.result_verified_at and attempt.score_source == SCORE_SOURCE and attempt.result_exam_code == exam.provider_exam_code and digest and attempt.result_identity_digest == digest)
        scored = bool(verified and attempt.score is not None and math.isfinite(attempt.score))
        status = "SCORED" if scored else "IDENTITY_MISSING" if not digest else "NEEDS_VERIFICATION" if attempt and attempt.status == "COMPLETED" else "AWAITING_RESULT" if done else "NOT_COMPLETED"
        row = {"student": assignment.student_id, "student_name": assignment.student.user.get_full_name() or assignment.student.user.username, "status": status, "completed": done, "score": attempt.score if scored else None, "quiz24_balance": attempt.balance if scored and attempt.balance is not None and math.isfinite(attempt.balance) else None, "provider_rank": attempt.rank if scored else None, "provider_rank_in_class": attempt.provider_rank_in_class if scored else None, "group_rank": None, "verified_at": attempt.result_verified_at if verified else None, "stale": bool(scored and now - attempt.result_verified_at > FRESHNESS), "details_available": bool(attempt and attempt.status == "COMPLETED")}
        rows.append(row)
        if scored:
            comparable.append((row, attempt))
    comparable.sort(key=lambda item: (-item[0]["score"], item[0]["student"]))
    previous, rank = None, 0
    for index, (row, _) in enumerate(comparable, 1):
        if previous is None or row["score"] != previous:
            rank = index
        row["group_rank"], previous = rank, row["score"]
    scores = [row["score"] for row, _ in comparable]
    balances = [row["quiz24_balance"] for row, _ in comparable if row["quiz24_balance"] is not None]
    counts = {}
    for key in ("correct_count", "wrong_count", "no_answer_count"):
        values = [getattr(attempt, key) for _, attempt in comparable if getattr(attempt, key) is not None]
        counts[key] = {"total": sum(values) if values else None, "average": statistics.mean(values) if values else None, "contributors": len(values)}
    metrics = {"assigned": len(assignments), "completed": completed, "valid_scored": len(scores), "participation_percentage": completed / len(assignments) * 100 if assignments else None, "average_score": statistics.mean(scores) if scores else None, "highest_score": max(scores) if scores else None, "lowest_score": min(scores) if scores else None, "median_score": float(statistics.median([Decimal(str(score)) for score in scores])) if scores else None, "average_quiz24_balance": statistics.mean(balances) if balances else None, "balance_contributors": len(balances), "answer_counts": counts, "distribution": distribution(scores)}
    state = ExamAnalyticsSync.objects.filter(exam=exam).first()
    eligible_ids = [row.pk for row in assignments if is_completed(row, attempts.get(row.student_id)) and result_identity(row.student, school)]
    remaining = sum(pk > state.cursor_assignment_id for pk in eligible_ids) if state else len(eligible_ids)
    metadata = {"metrics": metrics, "score_scale": {"source": "Quiz24 Score", "unit": "UNKNOWN", "comparable_within_exam": True, "label": "نمره خام Quiz24؛ مقیاس اعلام نشده"}, "ranking": {"method": "COMPETITION", "scope": "AMOOTECH_ASSIGNED_VERIFIED_SAME_EXAM", "provisional": True}, "last_success_at": state.last_success_at if state else None, "last_attempt_at": state.last_attempt_at if state else None, "fresh": bool(scores and all(not row["stale"] for row, _ in comparable) and len(scores) == completed and state and not state.error and not state.partial and not (state.lease_expires_at and state.lease_expires_at > now)), "fresh_until": min((attempt.result_verified_at + FRESHNESS for _, attempt in comparable), default=None), "partial": state.partial if state else True, "error": "بخشی از نتایج دریافت یا تأیید نشد؛ نتایج معتبر قبلی حفظ شدند." if state and state.error else "", "syncing": bool(state and state.lease_expires_at and state.lease_expires_at > now), "remaining_in_cycle": remaining, "batch_limit": BATCH_SIZE, "last_batch_size": state.last_batch_size if state else 0, "last_error_count": state.last_error_count if state else 0, "student_results_released": not exam.result_release_at or now >= exam.result_release_at}
    q, status, ordering = query.get("q", "").strip().casefold()[:100], query.get("status", ""), query.get("ordering", "rank")
    if q:
        rows = [row for row in rows if q in row["student_name"].casefold()]
    if status:
        rows = [row for row in rows if row["status"] == status]
    if ordering == "name":
        rows.sort(key=lambda row: (row["student_name"], row["student"]))
    elif ordering == "score_asc":
        rows.sort(key=lambda row: (row["score"] is None, row["score"] if row["score"] is not None else 0, row["student"]))
    else:
        rows.sort(key=lambda row: (row["group_rank"] is None, row["group_rank"] or 0, row["student"]))
    return rows, metadata


def sync_analytics(exam_id, user):
    now, token, school = timezone.now(), uuid.uuid4(), Quiz24Config().user_id
    with transaction.atomic():
        exam = Exam.objects.select_for_update().get(pk=exam_id, counselor__user=user, is_personal_practice=False)
        if not exam.provider_exam_code:
            return {"code": "MISSING_EXAM_CODE", "detail": "ابتدا کد آزمون را بررسی و تکمیل کنید."}, 409
        state, _ = ExamAnalyticsSync.objects.get_or_create(exam=exam)
        if state.lease_expires_at and state.lease_expires_at > now:
            return {"code": "SYNC_IN_PROGRESS", "detail": "دریافت نتایج در حال انجام است."}, 409
        if state.last_attempt_at and now - state.last_attempt_at < COOLDOWN:
            return {"code": "SYNC_COOLDOWN", "detail": "برای دریافت دوباره نتایج یک دقیقه فاصله لازم است."}, 429
        assignments, attempts = population(exam)
        eligible = [(row, identity(row.student, school)) for row in assignments if is_completed(row, attempts.get(row.student_id)) and identity(row.student, school)]
        batch = [row for row in eligible if row[0].pk > state.cursor_assignment_id][:BATCH_SIZE] or eligible[:BATCH_SIZE]
        if not batch:
            return {"code": "NO_ELIGIBLE_RESULTS", "detail": "شرکت‌کننده تکمیل‌شده با اتصال تأییدشده موجود نیست؛ ابتدا پایش شرکت‌کنندگان را بررسی کنید."}, 409
        claimed = ExamAnalyticsSync.objects.filter(pk=state.pk).filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).filter(Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=now - COOLDOWN)).update(lease_token=token, lease_expires_at=now + LEASE, last_attempt_at=now)
        if not claimed:
            return {"code": "SYNC_IN_PROGRESS", "detail": "درخواست دیگری در حال دریافت نتایج است."}, 409
        code, owner_id = exam.provider_exam_code, exam.counselor_id
    # HTTP is outside transactions. One documented result request per completed
    # student; at most ten per batch and thirty seconds of total network budget.
    service, began, processed, failures, successes = Quiz24Service(), time.monotonic(), [], 0, 0
    for assignment, mapping in batch:
        remaining = 30 - (time.monotonic() - began)
        if remaining <= 0:
            break
        processed.append(assignment.pk)
        try:
            response = service.get_exam_result(mapping[0], code, timeout=min(15.0, remaining))
            metrics, answers = strict_result(response, exam.question_type)
            with transaction.atomic():
                locked = Exam.objects.select_for_update().get(pk=exam_id)
                receipt = ExamAnalyticsSync.objects.select_for_update().get(exam_id=exam_id)
                student = StudentProfile.objects.select_for_update().get(pk=assignment.student_id)
                if receipt.lease_token != token or locked.provider_exam_code != code or locked.counselor_id != owner_id or locked.is_personal_practice or student.counselor_id != owner_id or identity(student, school) != mapping or not ExamAssignment.objects.filter(pk=assignment.pk, exam=locked, student=student).exists():
                    failures += 1
                    continue
                previous = attempts.get(student.pk)
                current_version = ExamAttempt.objects.filter(exam=locked, student=student).values_list("updated_at", flat=True).first()
                if current_version != (previous.updated_at if previous else None):
                    failures += 1
                    continue
                attempt, _ = ExamAttempt.objects.update_or_create(exam=locked, student=student, defaults={**metrics, "status": "COMPLETED", "provider_username": mapping[0], "result_verified_at": timezone.now(), "result_identity_digest": result_identity(student, school), "result_exam_code": code, "score_source": SCORE_SOURCE, "raw_result": {"examResult": {key: value for key, value in metrics.items() if value is not None}}})
                if answers:
                    attempt.answers.all().delete()
                    ExamAnswer.objects.bulk_create([ExamAnswer(attempt=attempt, **row) for row in answers])
                successes += 1
        except Exception:
            # Persist safe aggregate failure indicators, never provider PII/errors.
            failures += 1
    with transaction.atomic():
        locked = Exam.objects.select_for_update().get(pk=exam_id)
        state = ExamAnalyticsSync.objects.select_for_update().get(exam_id=exam_id)
        if state.lease_token != token:
            return {"code": "SYNC_SUPERSEDED", "detail": "نتیجه درخواست قدیمی کنار گذاشته شد."}, 409
        if locked.counselor_id != owner_id or locked.provider_exam_code != code or locked.is_personal_practice:
            failures += 1
        if processed:
            state.cursor_assignment_id = processed[-1]
        state.last_batch_size, state.last_error_count = len(processed), failures
        state.partial = bool(failures or len(processed) < len(batch) or any(row.pk > state.cursor_assignment_id for row, _ in eligible))
        state.error = "PARTIAL" if state.partial else ""
        if successes:
            state.last_success_at = timezone.now()
        state.lease_token, state.lease_expires_at = None, None
        state.save()
    return None, 200 if successes or not failures else 502
