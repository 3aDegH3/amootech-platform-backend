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
        self.full_clean()
        return super().save(*args, **kwargs)
