import hashlib
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework.exceptions import APIException, Throttled, ValidationError

from ..mobile import normalize_mobile
from ..models import MobileChallenge, MobileOTPQuota, StudentProfile


class DeliveryUnavailable(APIException):
    status_code = 503
    default_detail = "ارسال کد تأیید پیکربندی نشده یا در دسترس نیست؛ لطفاً با پشتیبانی تماس بگیرید."


class DevelopmentOTPAdapter:
    """Private local mailbox, read only by an explicit developer console command.

    Never writes OTPs to web responses, application logs or Docker stdout.
    """
    def send(self, mobile, code, *, student_id, nonce):
        if not settings.DEBUG or not settings.STUDENT_OTP_DEV_ENABLED:
            raise DeliveryUnavailable()
        directory = Path(settings.STUDENT_OTP_DEV_OUTBOX)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise DeliveryUnavailable()
        directory.chmod(0o700)
        fd = os.open(directory / f"{student_id}.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"code": code, "nonce": str(nonce)}, stream)


def adapter():
    path = settings.STUDENT_OTP_BACKEND
    if path:
        try:
            backend = import_string(path)()
            if isinstance(backend, DevelopmentOTPAdapter) and (not settings.DEBUG or not settings.STUDENT_OTP_DEV_ENABLED):
                raise DeliveryUnavailable()
            return backend
        except Exception:
            raise DeliveryUnavailable() from None
    if settings.DEBUG and settings.STUDENT_OTP_DEV_ENABLED:
        return DevelopmentOTPAdapter()
    raise DeliveryUnavailable()


def quota_rows(student_id, mobile, now):
    keys = sorted((f"student:{student_id}", "mobile:" + hashlib.sha256(mobile.encode()).hexdigest()))
    rows = []
    for key in keys:
        MobileOTPQuota.objects.get_or_create(key=key, defaults={"window_at": now})
        row = MobileOTPQuota.objects.select_for_update().get(key=key)
        if now - row.window_at >= timedelta(hours=1):
            row.window_at, row.sends, row.failures, row.sent_at = now, 0, 0, None
        rows.append(row)
    return rows


def request_code(student_id, mobile):
    try:
        mobile = normalize_mobile(mobile)
    except Exception:
        raise ValidationError({"mobile": "شماره موبایل معتبر نیست."}) from None
    sender = adapter()  # Fail before changing identity when SMS is not configured.
    now, nonce = timezone.now(), uuid4()
    code = f"{secrets.randbelow(1000000):06d}"
    try:
        with transaction.atomic():
            student = StudentProfile.objects.select_for_update().get(pk=student_id)
            rows = quota_rows(student_id, mobile, now)
            if any(row.sends >= 5 or row.failures >= 10 for row in rows):
                raise Throttled(3600, "تعداد درخواست‌ها بیش از حد مجاز است.")
            if any(row.sent_at and (now - row.sent_at).total_seconds() < 60 for row in rows):
                raise Throttled(60, "برای درخواست کد جدید یک دقیقه صبر کنید.")
            if StudentProfile.objects.filter(mobile=mobile).exclude(pk=student_id).exists():
                raise ValidationError({"mobile": "این شماره قابل استفاده نیست."})
            if student.mobile != mobile:
                student.mobile = mobile
                student.save()
            if student.mobile_verified_at:
                raise ValidationError({"mobile": "این شماره قبلاً تأیید شده است."})
            MobileChallenge.objects.update_or_create(student=student, defaults={"mobile": mobile, "nonce": nonce, "code_hash": make_password(code), "expires_at": now + timedelta(minutes=5), "attempts": 0, "consumed": False})
            for row in rows:
                row.sends += 1
                row.sent_at = now
                row.save()
    except (IntegrityError, DjangoValidationError):
        raise ValidationError({"mobile": "این شماره قابل استفاده نیست."}) from None
    # SMS delivery is outside all database transactions.
    try:
        sender.send(mobile, code, student_id=student_id, nonce=nonce)
    except Exception:
        MobileChallenge.objects.filter(student_id=student_id, nonce=nonce).update(consumed=True)
        raise DeliveryUnavailable() from None


def verify_code(student_id, code):
    now, failure = timezone.now(), None
    with transaction.atomic():
        student = StudentProfile.objects.select_for_update().get(pk=student_id)
        challenge = MobileChallenge.objects.select_for_update().filter(student=student).first()
        if not challenge or challenge.consumed or challenge.mobile != student.mobile or now >= challenge.expires_at or challenge.attempts >= 5:
            failure = "کد نامعتبر یا منقضی شده است؛ کد جدید درخواست کنید."
        else:
            rows = quota_rows(student_id, challenge.mobile, now)
            if any(row.failures >= 10 for row in rows):
                failure = "تعداد تلاش‌ها بیش از حد مجاز است؛ بعداً تلاش کنید."
            elif not check_password(str(code), challenge.code_hash):
                challenge.attempts += 1
                challenge.save(update_fields=("attempts",))
                for row in rows:
                    row.failures += 1
                    row.save()
                failure = "کد تأیید صحیح نیست."
            else:
                challenge.consumed = True
                challenge.code_hash = "!"
                challenge.save(update_fields=("consumed", "code_hash"))
                StudentProfile.objects.filter(pk=student_id).update(mobile_verified_at=now, quiz24_sync_status="PENDING", quiz24_sync_error="")
    # Raise after commit so failed attempts cannot be rolled back.
    if failure:
        raise ValidationError({"code": failure})
