from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.db import models


class UserManager(DjangoUserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("role", User.Role.ADMIN)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        COUNSELOR = "COUNSELOR", "Counselor"
        STUDENT = "STUDENT", "Student"

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.STUDENT)
    objects = UserManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=["ADMIN", "COUNSELOR", "STUDENT"]),
                name="accounts_user_valid_role",
            )
        ]


class CounselorProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.PROTECT, related_name="counselor_profile")

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.user_id and self.user.role != User.Role.COUNSELOR:
            raise ValidationError({"user": "User must have the COUNSELOR role."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class StudentProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.PROTECT, related_name="student_profile")
    counselor = models.ForeignKey(CounselorProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="students")
    grade = models.ForeignKey("academics.Grade", null=True, blank=True, on_delete=models.PROTECT)
    field = models.ForeignKey("academics.Field", null=True, blank=True, on_delete=models.PROTECT)
    school_name = models.CharField(max_length=150, blank=True)
    mobile = models.CharField(max_length=11, null=True, blank=True, unique=True)
    mobile_verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    quiz24_student_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    quiz24_school_id = models.CharField(max_length=10, blank=True, editable=False)
    quiz24_sync_status = models.CharField(max_length=16, default="NOT_READY", editable=False)
    quiz24_sync_error = models.CharField(max_length=250, blank=True, editable=False)
    quiz24_sync_at = models.DateTimeField(null=True, blank=True, editable=False)

    def clean(self):
        from django.core.exceptions import ValidationError
        errors = {}
        if self.user_id and self.user.role != User.Role.STUDENT:
            errors["user"] = "User must have the STUDENT role."
        if self.field_id and (not self.grade_id or self.field.grade_id != self.grade_id):
            errors["field"] = "Field must belong to the selected grade."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        from .mobile import normalize_mobile
        self.mobile = normalize_mobile(self.mobile) if self.mobile else None
        old = type(self).objects.filter(pk=self.pk).values("mobile").first() if self.pk else None
        changed = old is not None and old["mobile"] != self.mobile
        if changed or old is None:
            self.mobile_verified_at = None
            self.quiz24_student_id = None
            self.quiz24_school_id = ""
            self.quiz24_sync_status = "NOT_READY"
            self.quiz24_sync_error = ""
            self.quiz24_sync_at = None
            if kwargs.get("update_fields"):
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"mobile", "mobile_verified_at", "quiz24_student_id", "quiz24_school_id", "quiz24_sync_status", "quiz24_sync_error", "quiz24_sync_at"}
        self.full_clean()
        result = super().save(*args, **kwargs)
        if changed:
            MobileChallenge.objects.filter(student=self).delete()
        return result


class MobileChallenge(models.Model):
    student = models.OneToOneField(StudentProfile, on_delete=models.CASCADE)
    mobile = models.CharField(max_length=11)
    code_hash = models.CharField(max_length=256)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed = models.BooleanField(default=False)
    nonce = models.UUIDField()


class MobileOTPQuota(models.Model):
    key = models.CharField(max_length=80, unique=True)
    window_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True)
    sends = models.PositiveSmallIntegerField(default=0)
    failures = models.PositiveSmallIntegerField(default=0)


class Quiz24StudentRegistration(models.Model):
    student = models.ForeignKey(StudentProfile, null=True, on_delete=models.SET_NULL)
    school_id = models.CharField(max_length=10)
    mobile = models.CharField(max_length=11)
    state = models.CharField(max_length=16, default="PENDING")
    external_id = models.PositiveBigIntegerField(null=True)
    error = models.CharField(max_length=250, blank=True)
    attempted_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("school_id", "mobile"), name="accounts_quiz24_school_mobile")]
