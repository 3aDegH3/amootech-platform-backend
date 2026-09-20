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
