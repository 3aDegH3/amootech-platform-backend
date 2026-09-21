import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import User, CounselorProfile, StudentProfile
from apps.academics.models import Grade, Field, Subject, Chapter, Topic


DEMO_PASSWORD = "Demo12345!"


class Command(BaseCommand):
    help = "Create local Amootech demo data"

    def handle(self, *args, **options):
        if os.getenv("ALLOW_DEMO_SEED", "").lower() not in {"1", "true", "yes"}:
            raise CommandError(
                "Demo seed is disabled. Set ALLOW_DEMO_SEED=true first."
            )

        with transaction.atomic():
            self._create_demo_data()

        self.stdout.write(
            self.style.SUCCESS("Demo data created successfully.")
        )

        self.stdout.write("")
        self.stdout.write("Admin:")
        self.stdout.write("  username: demo_admin")
        self.stdout.write(f"  password: {DEMO_PASSWORD}")

        self.stdout.write("")
        self.stdout.write("Counselor:")
        self.stdout.write("  username: demo_counselor")
        self.stdout.write(f"  password: {DEMO_PASSWORD}")

        self.stdout.write("")
        self.stdout.write("Student:")
        self.stdout.write("  username: demo_student")
        self.stdout.write(f"  password: {DEMO_PASSWORD}")

    def _ensure_user(
        self,
        *,
        username,
        role,
        first_name,
        last_name,
        email,
    ):
        user, _ = User.objects.get_or_create(
            username=username,
            defaults={
                "role": role,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "is_active": True,
            },
        )

        user.role = role
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.is_active = True
        user.set_password(DEMO_PASSWORD)

        if role == User.Role.ADMIN:
            user.is_staff = True
            user.is_superuser = True

        user.save()

        return user

    def _create_demo_data(self):
        # ---------------------------------
        # Academic structure
        # ---------------------------------

        grade, _ = Grade.objects.get_or_create(
            name="دوازدهم",
            defaults={
                "ordering": 12,
                "is_active": True,
            },
        )

        field, _ = Field.objects.get_or_create(
            grade=grade,
            name="ریاضی",
            defaults={
                "ordering": 1,
                "is_active": True,
            },
        )

        subject, _ = Subject.objects.get_or_create(
            field=field,
            name="حسابان",
            defaults={
                "ordering": 1,
                "is_active": True,
            },
        )

        chapter, _ = Chapter.objects.get_or_create(
            subject=subject,
            name="معادلات",
            defaults={
                "ordering": 1,
                "is_active": True,
            },
        )

        Topic.objects.get_or_create(
            chapter=chapter,
            name="معادله درجه دوم",
            defaults={
                "ordering": 1,
                "is_active": True,
            },
        )

        # ---------------------------------
        # Admin
        # ---------------------------------

        self._ensure_user(
            username="demo_admin",
            role=User.Role.ADMIN,
            first_name="ادمین",
            last_name="آموتک",
            email="admin@demo.local",
        )

        # ---------------------------------
        # Counselor
        # ---------------------------------

        counselor_user = self._ensure_user(
            username="demo_counselor",
            role=User.Role.COUNSELOR,
            first_name="محمد",
            last_name="رضایی",
            email="counselor@demo.local",
        )

        counselor_profile, _ = CounselorProfile.objects.get_or_create(
            user=counselor_user,
        )

        # ---------------------------------
        # Student
        # ---------------------------------

        student_user = self._ensure_user(
            username="demo_student",
            role=User.Role.STUDENT,
            first_name="علی",
            last_name="احمدی",
            email="student@demo.local",
        )

        StudentProfile.objects.update_or_create(
            user=student_user,
            defaults={
                "counselor": counselor_profile,
                "grade": grade,
                "field": field,
                "school_name": "دبیرستان نمونه آموتک",
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Academic hierarchy + admin + counselor + student created."
            )
        )