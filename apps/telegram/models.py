import hashlib
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TelegramGroupConnection(models.Model):
    """Business truth: which Telegram group is linked to which student."""

    student = models.ForeignKey(
        "accounts.StudentProfile",
        on_delete=models.CASCADE,
        related_name="telegram_group_connections",
    )
    telegram_chat_id = models.BigIntegerField()
    chat_title = models.CharField(max_length=255, blank=True)
    chat_type = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Telegram Group Connection"
        ordering = ("-connected_at",)
        constraints = [
            models.UniqueConstraint(fields=["student"], condition=models.Q(is_active=True), name="telegram_one_active_group_per_student"),
            models.UniqueConstraint(fields=["telegram_chat_id"], condition=models.Q(is_active=True), name="telegram_one_student_per_group"),
        ]

    def clean(self):
        errors = {}
        if self.telegram_chat_id == 0:
            errors["telegram_chat_id"] = "Chat ID cannot be zero."
        # When is_active=False, allow duplicates at model level (DB unique still prevents two active, but inactive can exist)
        # We use conditional unique via logic in view, but model OneToOne prevents any dup. So allow via custom check:
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Skip full_clean for unique checks when inactive — handled in views transactionally
        # Still validate telegram_chat_id
        if self.telegram_chat_id == 0:
            raise ValidationError({"telegram_chat_id": "Chat ID cannot be zero."})
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Group {self.telegram_chat_id} -> Student {self.student_id}"


class TelegramStudentConnection(models.Model):
    """Business truth: which Telegram user is linked to which student."""

    student = models.ForeignKey(
        "accounts.StudentProfile",
        on_delete=models.CASCADE,
        related_name="telegram_student_connections",
    )
    telegram_user_id = models.BigIntegerField()
    telegram_username = models.CharField(max_length=64, blank=True, null=True)
    telegram_first_name = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_enabled = models.BooleanField(default=True)
    is_suspended = models.BooleanField(default=False)
    is_banned = models.BooleanField(default=False)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Telegram Student Connection"
        ordering = ("-connected_at",)
        constraints = [
            models.UniqueConstraint(fields=["student"], condition=models.Q(is_active=True), name="telegram_one_active_user_per_student"),
            models.UniqueConstraint(fields=["telegram_user_id"], condition=models.Q(is_active=True), name="telegram_one_student_per_user"),
        ]

    def save(self, *args, **kwargs):
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"User {self.telegram_user_id} -> Student {self.student_id}"


class TelegramAdminActionLog(models.Model):
    class Action(models.TextChoices):
        ENABLE = "ENABLE", "Enable"
        DISABLE = "DISABLE", "Disable"
        SUSPEND = "SUSPEND", "Suspend"
        RESUME = "RESUME", "Resume"
        BAN = "BAN", "Ban"
        UNBAN = "UNBAN", "Unban"
        GROUP_LOCK = "GROUP_LOCK", "Group lock"
        GROUP_UNLOCK = "GROUP_UNLOCK", "Group unlock"
        RESEND_PLAN = "RESEND_PLAN", "Resend plan"

    actor = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, related_name="telegram_admin_actions")
    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.CASCADE, related_name="telegram_admin_logs")
    group_connection = models.ForeignKey(TelegramGroupConnection, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    reason = models.CharField(max_length=500, blank=True)
    success = models.BooleanField(default=True)
    details = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Telegram Admin Action Log"


class TelegramConnectionToken(models.Model):
    """One-time, hashed token for startgroup group connection."""

    student = models.ForeignKey(
        "accounts.StudentProfile",
        on_delete=models.CASCADE,
        related_name="telegram_connection_tokens",
    )
    token_hash = models.CharField(max_length=128, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_telegram_tokens",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    @classmethod
    def create_for_student(cls, *, student, created_by, ttl_seconds: int = 900):
        """
        Generate a URL-safe random token (<=64 chars for startgroup) and store only its hash.
        Returns (instance, raw_token).
        """
        raw = secrets.token_urlsafe(32)
        # token_urlsafe(32) => ~43 chars, well under 64 limit for startgroup param value
        hashed = _hash_token(raw)
        instance = cls.objects.create(
            student=student,
            token_hash=hashed,
            expires_at=timezone.now() + timezone.timedelta(seconds=ttl_seconds),
            created_by=created_by,
        )
        return instance, raw

    @classmethod
    def hash_of(cls, raw: str) -> str:
        return _hash_token(raw)

    def save(self, *args, **kwargs):
        if not self.token_hash:
            raise ValidationError({"token_hash": "token_hash is required."})
        return super().save(*args, **kwargs)
