"""Local lifecycle and fenced, optional operations on the SAME provider exam."""
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..configuration import Quiz24Config, valid_http_url
from ..exceptions import Quiz24Error
from ..models import Exam, ExamManagementEvent, ExamProviderOperation
from ..serializers import SyncedQuiz24ExamSerializer
from .monitoring import parse_participants
from .quiz24 import Quiz24Service


def event(exam, actor, action, previous=None, current=None, outcome="SUCCESS"):
    ExamManagementEvent.objects.create(exam=exam, actor=actor, action=action, previous=previous or {}, current=current or {}, outcome=outcome)


def activity(exam):
    return exam.attempts.exists() or exam.assignments.filter(participation__status__in=("IN_PROGRESS", "COMPLETED")).exists()


def operation(exam):
    return ExamProviderOperation.objects.filter(exam=exam).first()


def blocked(exam):
    op = operation(exam)
    return bool(op and op.state in ("PENDING", "EDITING", "UNKNOWN", "DELETED"))


def require_mutable(exam):
    if exam.archived_at or exam.trashed_at or blocked(exam):
        raise ValidationError({"detail": "آزمون بایگانی، در سطل زباله یا دارای عملیات حل‌نشده سرویس است."})


def snapshot(exam):
    return {"title": exam.title, "status": exam.status, "archived_at": exam.archived_at.isoformat() if exam.archived_at else None, "trashed_at": exam.trashed_at.isoformat() if exam.trashed_at else None}


def local_action(exam, actor, action, expected, title=None):
    """Caller locks exam. Idempotent targets return unchanged without audit spam."""
    if expected is None or exam.updated_at != expected:
        raise ValidationError({"detail": "اطلاعات انتخاب‌شده قدیمی است؛ فهرست را بازخوانی کنید."})
    before = snapshot(exam)
    if blocked(exam):
        raise ValidationError({"detail": "ابتدا عملیات یا وضعیت نامطمئن سرویس را بررسی و تطبیق دهید."})
    if action == "rename":
        if exam.trashed_at:
            raise ValidationError({"detail": "ابتدا آزمون را از سطل زباله بازیابی کنید."})
        exam.title = title
    elif action == "archive":
        if exam.trashed_at:
            raise ValidationError({"detail": "آزمون در سطل زباله است."})
        exam.archived_at = exam.archived_at or timezone.now()
    elif action == "unarchive":
        if exam.trashed_at:
            raise ValidationError({"detail": "ابتدا از سطل زباله بازیابی کنید."})
        if exam.archived_at:
            exam.archived_at = None
            if exam.status == Exam.Status.PUBLISHED:
                exam.status = Exam.Status.CLOSED if activity(exam) else Exam.Status.READY
    elif action == "trash":
        exam.trashed_at = exam.trashed_at or timezone.now()
    elif action == "restore":
        if exam.trashed_at:
            exam.trashed_at = None
            if exam.status == Exam.Status.PUBLISHED:
                exam.status = Exam.Status.CLOSED if activity(exam) else Exam.Status.READY
    elif action == "unpublish":
        if exam.archived_at or exam.trashed_at:
            raise ValidationError({"detail": "آزمون فعال نیست."})
        if exam.status == Exam.Status.READY:
            return exam
        if exam.status != Exam.Status.PUBLISHED or activity(exam):
            raise ValidationError({"detail": "لغو انتشار فقط پیش از هرگونه مشارکت یا تلاش مجاز است."})
        exam.status = Exam.Status.READY
    else:
        raise ValidationError({"detail": "عملیات معتبر نیست."})
    after = snapshot(exam)
    if before == after:
        return exam
    exam.save(update_fields=("title", "status", "archived_at", "trashed_at", "updated_at"))
    event(exam, actor, action.upper(), before, after)
    return exam


def capabilities(exam, actor):
    op, has_activity = operation(exam), activity(exam)
    busy = bool(op and op.state in ("PENDING", "EDITING", "UNKNOWN", "DELETED"))
    active = not exam.archived_at and not exam.trashed_at and not busy
    manual = exam.creation_mode == "MANUAL"
    return {
        "has_activity": has_activity, "busy": busy,
        "can_publish": bool(active and exam.status == "READY" and exam.provider_exam_code and exam.question_count and exam.question_count > 0),
        "can_edit_questions": bool(active and manual and not exam.ever_published and exam.status in ("DRAFT", "READY") and not has_activity and (exam.provider_exam_id or exam.manage_url)),
        "edit_explanation": "ویرایش سوال فقط برای آزمون دستیِ هرگز منتشرنشده و پیش از هرگونه تلاش مجاز است؛ لغو لینک قدیمی و سازگاری آزمون خودکار تأیید نشده است.",
        "can_unpublish": bool(active and exam.status == "PUBLISHED" and not has_activity),
        "remote_delete_enabled": bool(getattr(settings, "QUIZ24_REMOTE_DELETE_ENABLED", False)),
        "can_remote_delete": bool(getattr(settings, "QUIZ24_REMOTE_DELETE_ENABLED", False) and not busy and exam.trashed_at and not exam.ever_published and exam.status in ("DRAFT", "READY") and not has_activity and not exam.assignments.exists() and exam.provider_exam_id and exam.provider_exam_code),
        "reset_supported": False, "reset_explanation": "بازنشانی پشتیبانی نمی‌شود؛ نوع شناور و قابلیت ویرایش پاسخنامه در اطلاعات فعلی قابل تأیید نیست. هیچ تلاش یا نتیجه‌ای حذف نمی‌شود.",
        "operation": {"kind": op.kind, "state": op.state, "edit_session": str(op.token) if op.kind == "EDIT" and op.actor_id == actor.pk else None} if op else None,
    }


def verify_provider(service, exam):
    provider = SyncedQuiz24ExamSerializer.from_provider(service.get_exam(exam.provider_exam_code))
    if not provider.is_valid() or provider.validated_data["provider_exam_id"] != exam.provider_exam_id or provider.validated_data.get("provider_exam_code", exam.provider_exam_code) != exam.provider_exam_code:
        raise Quiz24Error(None, "شناسه آزمون سرویس با آزمون محلی تطبیق ندارد.", api_status=409)
    began, pages = time.monotonic(), set()
    for page in range(1, 21):
        remaining = 30 - (time.monotonic() - began)
        if remaining <= 0:
            raise Quiz24Error(None, "پوشش مشارکت سرویس قابل تأیید نیست.", api_status=409)
        response = service.get_exam_participants(exam.provider_exam_code, page, timeout=min(15, remaining))
        rows, partial = parse_participants(response)
        if partial or any(status != "NOT_STARTED" for status in rows.values()):
            raise Quiz24Error(None, "مشارکت یا اطلاعات نامطمئن سرویس مانع عملیات است.", api_status=409)
        if not response["result"]:
            return
        signature = tuple(sorted(rows))
        if signature in pages:
            break
        pages.add(signature)
    raise Quiz24Error(None, "پوشش مشارکت سرویس قابل تأیید نیست.", api_status=409)


def provider_action(exam_id, actor, kind, request_key, expected, confirmed_id=None, confirmed_title=None, confirmed=False):
    token = uuid.UUID(str(request_key))
    with transaction.atomic():
        exam = Exam.objects.select_for_update().get(pk=exam_id, counselor__user=actor, is_personal_practice=False)
        op = operation(exam)
        if op and op.token == token:
            if op.kind == kind and op.actor_id == actor.pk and op.state == "EDITING":
                return {"edit_url": op.response_url, "edit_session": str(token)}, 200
            if op.kind == kind and op.state == "DELETED":
                return {"state": "DELETED"}, 200
            return {"detail": "این درخواست قبلاً ثبت شده؛ وضعیت آن را بررسی کنید."}, 409
        if blocked(exam):
            return {"detail": "عملیات قبلی سرویس باید بررسی و تطبیق داده شود."}, 409
        if expected is None or exam.updated_at != expected:
            return {"detail": "نسخه آزمون تغییر کرده؛ اطلاعات را بازخوانی کنید."}, 409
        if activity(exam):
            return {"detail": "وجود تلاش یا مشارکت، ویرایش یا حذف سرویس را ممنوع می‌کند."}, 409
        if kind == "EDIT":
            if exam.creation_mode != "MANUAL" or exam.ever_published or exam.status not in ("DRAFT", "READY") or exam.archived_at or exam.trashed_at:
                return {"detail": "ویرایش سوال برای این آزمون مجاز یا تأییدشده نیست."}, 409
            if not exam.provider_exam_id and not valid_http_url(exam.manage_url):
                return {"detail": "ابتدا مشخصات آزمون موجود را همگام‌سازی کنید."}, 409
        else:
            if not getattr(settings, "QUIZ24_REMOTE_DELETE_ENABLED", False):
                return {"detail": "حذف از Quiz24 در تنظیمات سامانه غیرفعال است."}, 403
            if not confirmed or confirmed_id != exam.provider_exam_id or confirmed_title != exam.title:
                return {"detail": "تأیید جداگانه، عنوان دقیق و شناسه منطبق آزمون لازم است."}, 400
            if not exam.trashed_at or exam.ever_published or exam.status not in ("DRAFT", "READY") or exam.assignments.exists() or not exam.provider_exam_id or not exam.provider_exam_code:
                return {"detail": "حذف سرویس فقط برای آزمون هرگز منتشرنشده، بدون تخصیص و در سطل زباله مجاز است."}, 409
        op, _ = ExamProviderOperation.objects.update_or_create(exam=exam, defaults={"kind": kind, "state": "PENDING", "token": token, "actor": actor, "response_url": ""})
        exam.save(update_fields=("updated_at",))
        event(exam, actor, "EDIT_REQUEST" if kind == "EDIT" else "REMOTE_DELETE_REQUEST", outcome="PENDING")
        version, provider_id, code = exam.updated_at, exam.provider_exam_id, exam.provider_exam_code
    # No database transaction spans provider HTTP. A durable claim blocks other
    # management/start mutations, and ambiguous outcomes are never blindly retried.
    mutation_sent, state, url = False, "FAILED", ""
    try:
        service = Quiz24Service()
        if provider_id:
            verify_provider(service, exam)
        if kind == "EDIT" and not provider_id:
            url, state = exam.manage_url, "EDITING"
        else:
            # Recheck claims/local activity after read-only preflight, before mutation.
            with transaction.atomic():
                current = Exam.objects.select_for_update().get(pk=exam_id)
                receipt = ExamProviderOperation.objects.select_for_update().get(exam_id=exam_id)
                if receipt.token != token or current.updated_at != version or activity(current):
                    raise Quiz24Error(None, "اطلاعات محلی در زمان بررسی تغییر کرده است.")
            mutation_sent = True
            response = service.edit_existing_exam(provider_id, Quiz24Config().manual_callback(exam_id, token)) if kind == "EDIT" else service.delete_existing_exam(provider_id)
            if not isinstance(response, dict) or str(response.get("status")) != "100":
                raise Quiz24Error(None, "پاسخ عملیات قابل تأیید نیست.")
            if kind == "EDIT":
                url = response.get("result")
                if not valid_http_url(url) or str(response.get("examCode", code)) != str(code):
                    raise Quiz24Error(None, "لینک و هویت ویرایش قابل تأیید نیست.")
                state = "EDITING"
            else:
                state = "DELETED"
    except Quiz24Error as error:
        definite_rejection = str(error.status) in ("101", "103", "109", "110", "114", "115")
        state = "UNKNOWN" if mutation_sent and not definite_rejection else "FAILED"
    except Exception:
        state = "UNKNOWN" if mutation_sent else "FAILED"
    with transaction.atomic():
        current = Exam.objects.select_for_update().get(pk=exam_id)
        op = ExamProviderOperation.objects.select_for_update().get(exam_id=exam_id)
        if op.token != token:
            return {"detail": "پاسخ درخواست قدیمی کنار گذاشته شد."}, 409
        if current.updated_at != version or current.provider_exam_id != provider_id or current.provider_exam_code != code:
            state = "UNKNOWN"
        op.state, op.response_url = state, url if state == "EDITING" else ""
        op.save()
        event(current, actor, "EDIT_READY" if kind == "EDIT" else "REMOTE_DELETE_OUTCOME", current={"state": state}, outcome=state)
    if state == "EDITING":
        return {"edit_url": url, "edit_session": str(token)}, 200
    if state == "DELETED":
        return {"state": state}, 200
    return {"state": state, "detail": "نتیجه سرویس نامطمئن است؛ تکرار خودکار ممنوع و بررسی لازم است." if state == "UNKNOWN" else "عملیات سرویس تأیید نشد؛ هیچ موفقیتی اعلام نشده است."}, 502
