import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.exams.configuration import Quiz24Config
from apps.exams.exceptions import Quiz24Error
from apps.exams.services.quiz24 import Quiz24Service
from ..models import Quiz24StudentRegistration, StudentProfile


ERRORS = {
    "CONFLICT": "حسابی با این شماره در Quiz24 وجود دارد؛ برای تطبیق امن حساب با پشتیبانی تماس بگیرید.",
    "UNKNOWN": "نتیجه ثبت حساب مشخص نیست؛ برای جلوگیری از حساب تکراری، نیاز به بررسی پشتیبانی است.",
    "FAILED": "ثبت حساب آزمون ناموفق بود؛ می‌توانید دوباره تلاش کنید.",
}


def sync_student(student_id):
    """Short DB claims surround, but never contain, a provider network request."""
    now = timezone.now()
    config = Quiz24Config()
    try:
        config.validate(require_user=True)
    except Quiz24Error:
        StudentProfile.objects.filter(pk=student_id, mobile_verified_at__isnull=False).update(quiz24_sync_status="FAILED", quiz24_sync_error="اتصال سرویس آزمون پیکربندی نشده است.", quiz24_sync_at=now)
        return
    with transaction.atomic():
        student = StudentProfile.objects.select_for_update().select_related("user").get(pk=student_id)
        if not student.mobile or not student.mobile_verified_at:
            return
        mobile, verified_at = student.mobile, student.mobile_verified_at
        snapshot = {"pk": student_id, "mobile": mobile, "mobile_verified_at": verified_at}
        if not student.user.first_name.strip() or not student.user.last_name.strip():
            StudentProfile.objects.filter(**snapshot).update(quiz24_sync_status="FAILED", quiz24_sync_error="ابتدا نام و نام خانوادگی را در پروفایل تکمیل کنید.", quiz24_sync_at=now)
            return
        receipt, _ = Quiz24StudentRegistration.objects.get_or_create(school_id=config.user_id, mobile=mobile, defaults={"student": student})
        receipt = Quiz24StudentRegistration.objects.select_for_update().get(pk=receipt.pk)
        if receipt.student_id != student_id:
            StudentProfile.objects.filter(**snapshot).update(quiz24_sync_status="CONFLICT", quiz24_sync_error=ERRORS["CONFLICT"], quiz24_sync_at=now)
            return
        if receipt.state == "SYNCED":
            StudentProfile.objects.filter(**snapshot).update(quiz24_student_id=receipt.external_id, quiz24_school_id=config.user_id, quiz24_sync_status="SYNCED", quiz24_sync_error="", quiz24_sync_at=now)
            return
        if receipt.state == "SYNCING":
            if receipt.attempted_at and now - receipt.attempted_at > timedelta(minutes=2):
                receipt.state = "UNKNOWN"
                receipt.error = ERRORS["UNKNOWN"]
                receipt.save(update_fields=("state", "error"))
            else:
                return
        if receipt.state in ("CONFLICT", "UNKNOWN"):
            StudentProfile.objects.filter(**snapshot).update(quiz24_sync_status=receipt.state, quiz24_sync_error=receipt.error, quiz24_sync_at=now)
            return
        if receipt.attempted_at and now - receipt.attempted_at < timedelta(seconds=60):
            return
        receipt.state, receipt.attempted_at, receipt.error = "SYNCING", now, ""
        receipt.save(update_fields=("state", "attempted_at", "error"))
        StudentProfile.objects.filter(**snapshot).update(quiz24_sync_status="SYNCING", quiz24_sync_error="", quiz24_sync_at=now)
        first_name, last_name = student.user.first_name, student.user.last_name
    # Credentials are independent of Amootech login, never persisted or exposed.
    password = secrets.token_urlsafe(32)
    external_id, state, error = None, "UNKNOWN", ERRORS["UNKNOWN"]
    try:
        response = Quiz24Service().register_student(mobile=mobile, first_name=first_name, last_name=last_name, password=password)
        result = response.get("result")
        value = response.get("userId", result.get("userId") if isinstance(result, dict) else None)
        if type(value) is int and value > 0 and value != int(config.user_id):
            external_id, state, error = value, "SYNCED", ""
    except Quiz24Error as exc:
        if str(exc.status) == "104":
            state = "CONFLICT"
        elif exc.status is not None and str(exc.status) != "100":
            state = "FAILED"
        error = ERRORS[state]
    except Exception:
        # No secret/provider body is logged; an ambiguous result cannot be retried.
        pass
    with transaction.atomic():
        Quiz24StudentRegistration.objects.filter(pk=receipt.pk).update(state=state, error=error, external_id=external_id)
        StudentProfile.objects.filter(**snapshot).update(quiz24_sync_status=state, quiz24_sync_error=error, quiz24_sync_at=timezone.now(), quiz24_student_id=external_id, quiz24_school_id=config.user_id if state == "SYNCED" else "")
