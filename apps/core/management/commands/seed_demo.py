"""Guarded, deterministic development data from the existing product models."""

import json
from pathlib import Path
from django.conf import settings
import os
import re
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.academics.models import Grade, Field
from apps.exams.services.catalog import taxonomy_index
from apps.planning.titles import build_title
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution

DEMO_PASSWORD = "Demo12345!"
COUNSELORS = (("محمد", "رضایی"), ("مریم", "کاظمی"), ("علی", "محمدی"), ("سارا", "حسینی"), ("رضا", "احمدی"))
FIRST_NAMES = ("علی", "زهرا", "امیر", "فاطمه", "محمد", "سارا", "آرمان", "مریم", "پارسا", "نگار", "کیان", "نرگس")
LAST_NAMES = ("احمدی", "محمدی", "رضایی", "حسینی", "کریمی", "جعفری", "موسوی", "صادقی", "اکبری", "مرادی", "قاسمی", "عباسی")
EVENTS = ("باشگاه", "مدرسه", "کلاس", "رفت‌وآمد")
RESOURCES = ("کتاب تست", "جزوه", "ویدیو جلسه ۵", "تکلیف مدرسه", "آزمون آزمایشی")
COUNSELOR_PATTERN = re.compile(r"demo_counselor(?:_\d{2})?\Z")
STUDENT_PATTERN = re.compile(r"demo_student(?:_\d{3,4})?\Z")


def counselor_name(index):
    return "demo_counselor" if index == 1 else f"demo_counselor_{index:02d}"


def student_name(index):
    return "demo_student" if index == 1 else f"demo_student_{index:03d}"


def saturday(date):
    return date - timedelta(days=(date.weekday() + 2) % 7)


class Command(BaseCommand):
    help = "Create a guarded, deterministic local demo dataset."

    def add_arguments(self, parser):
        parser.add_argument("--counselors", type=int, default=5)
        parser.add_argument("--students-per-counselor", type=int, default=10)
        parser.add_argument("--history-days", type=int, default=30)
        parser.add_argument("--reset", action="store_true")

    def handle(self, *args, **options):
        if os.getenv("ALLOW_DEMO_SEED", "").lower() not in {"1", "true", "yes"}:
            raise CommandError("Demo seed is disabled. Set ALLOW_DEMO_SEED=true first.")
        counselor_count, per, history = (options[key] for key in ("counselors", "students_per_counselor", "history_days"))
        if not 1 <= counselor_count <= 20 or not 1 <= per <= 50 or not 1 <= history <= 90 or counselor_count * per > 200:
            raise CommandError("Use 1–20 counselors, 1–50 students each (at most 200 total), and 1–90 history days.")
        if not settings.DEBUG:
            raise CommandError("Demo seed requires DEBUG=true on a development database.")
        self._load_catalog()
        with transaction.atomic():
            reserved = User.objects.filter(username__startswith='demo_')
            for user in reserved:
                if (user.username == 'demo_admin' or COUNSELOR_PATTERN.fullmatch(user.username)
                        or STUDENT_PATTERN.fullmatch(user.username)) and not user.email.endswith('@demo.local'):
                    raise CommandError('Reserved username is not marked @demo.local; refusing to modify it.')
            existing = self._demo_names()
            self._clear_transactions(existing["students"])
            if options["reset"]:
                self._delete_accounts(existing, existing["students"])
            profiles = self._ensure_people(counselor_count, per)
            if not options["reset"]:
                self._delete_accounts({
                    "admins": set(),
                    "counselors": existing["counselors"] - {counselor_name(i) for i in range(1, counselor_count + 1)},
                    "students": existing["students"] - {student_name(i) for i in range(1, len(profiles) + 1)},
                }, existing["students"])
            self._create_activity(profiles, history)
            counts = self._counts({student_name(i) for i in range(1, len(profiles) + 1)})
        self.stdout.write(self.style.SUCCESS("Demo dataset created"))
        for label, count in counts.items():
            self.stdout.write(f"{label}: {count}")
        self.stdout.write(f"\nAdmin: demo_admin / {DEMO_PASSWORD}")
        self.stdout.write(f"Counselor example: demo_counselor / {DEMO_PASSWORD}")
        self.stdout.write(f"Student example: demo_student / {DEMO_PASSWORD}")

    def _demo_names(self):
        result = {"admins": set(), "counselors": set(), "students": set()}
        for name, role in User.objects.filter(username__startswith="demo_").values_list("username", "role"):
            if name == "demo_admin" and role == User.Role.ADMIN:
                result["admins"].add(name)
            elif COUNSELOR_PATTERN.fullmatch(name) and role == User.Role.COUNSELOR:
                result["counselors"].add(name)
            elif STUDENT_PATTERN.fullmatch(name) and role == User.Role.STUDENT:
                result["students"].add(name)
        return result

    def _clear_transactions(self, names):
        if not names:
            return
        owner = {"student__user__username__in": names}
        # ReportItem.plan_item is PROTECT: delete reports before plans.
        DailyReport.objects.filter(**owner).delete()
        PlanItemExecution.objects.filter(**owner).delete()
        Plan.objects.filter(**owner).delete()

    def _delete_accounts(self, groups, all_demo_students):
        counselors = groups["counselors"]
        if counselors and (
            StudentProfile.objects.filter(counselor__user__username__in=counselors)
            .exclude(user__username__in=all_demo_students).exists()
            or Plan.objects.filter(counselor__user__username__in=counselors)
            .exclude(student__user__username__in=all_demo_students).exists()
        ):
            raise CommandError("A demo counselor is referenced by non-demo data; reset was rolled back.")
        StudentProfile.objects.filter(user__username__in=groups["students"]).delete()
        CounselorProfile.objects.filter(user__username__in=counselors).delete()
        for names in groups.values():
            if names:
                User.objects.filter(username__in=names).delete()

    def _user(self, username, role, first, last, email):
        user, _ = User.objects.get_or_create(username=username, defaults={"role": role})
        if user.role != role:
            raise CommandError(f"Reserved demo username {username} has a different role.")
        user.first_name, user.last_name, user.email = first, last, email
        user.is_active = True
        user.set_password(DEMO_PASSWORD)
        if role == User.Role.ADMIN:
            user.is_staff = user.is_superuser = True  # Existing local convenience only.
        user.save()
        return user

    def _ensure_people(self, count, per):
        self._user("demo_admin", User.Role.ADMIN, "ادمین", "آموتک", "admin@demo.local")
        counselors = []
        for index in range(1, count + 1):
            first, last = COUNSELORS[(index - 1) % len(COUNSELORS)]
            user = self._user(counselor_name(index), User.Role.COUNSELOR, first, last, f"counselor{index}@demo.local")
            profile, _ = CounselorProfile.objects.get_or_create(user=user)
            counselors.append(profile)
        grade, _ = Grade.objects.get_or_create(number=12, defaults={'name': 'دوازدهم', 'ordering': 12})
        field, _ = Field.objects.get_or_create(grade=grade, name='تجربی')
        if grade.name != 'دوازدهم' or not grade.is_active or not field.is_active:
            raise CommandError('Canonical grade/field is unavailable; existing data was not changed.')
        profiles = []
        for index in range(1, count * per + 1):
            first = FIRST_NAMES[(index - 1) % len(FIRST_NAMES)]
            last = LAST_NAMES[(index - 1 + (index - 1) // len(FIRST_NAMES)) % len(LAST_NAMES)]
            user = self._user(student_name(index), User.Role.STUDENT, first, last, f"student{index:03d}@demo.local")
            profile, _ = StudentProfile.objects.update_or_create(user=user, defaults={
                "counselor": counselors[(index - 1) // per], "grade": field.grade, "field": field,
                "school_name": "دبیرستان نمونه آموتک",
            })
            profiles.append(profile)
        return profiles

    def _load_catalog(self):
        # Authentic read-only capture, kept with its capture date. No invented IDs.
        capture = json.loads((Path(__file__).resolve().parents[2] / 'demo_catalog.json').read_text())
        grade = capture['grade']
        self.selections = []
        grade_row = next(row for row in capture['grades']['result'] if row['id'] == grade)
        for course, detail in capture['details'].items():
            course = int(course)
            course_row = next(row for row in capture['courses']['result']
                              if row['gradeId'] == grade and row['courseId'] == course)
            index = taxonomy_index(detail, course, grade)
            names = {}
            def visit(nodes):
                for node in nodes:
                    names[node['key']] = node['title']
                    visit(node.get('children', []))
            visit(detail['result'])
            for level in ('chapter', 'subject', 'object'):
                if not index[level]:
                    continue
                path = next(iter(index[level].values()))
                ids = {'grade': grade, **path}
                labels = {key: names[value] for key, value in path.items()}
                labels.update(grade=grade_row['title'], course=course_row['courseName'])
                self.selections.append({'provider': 'quiz24', **ids, 'labels': labels})
        if len({row['course'] for row in self.selections}) < 2:
            raise CommandError('At least two captured courses are required.')
        # Model saves intentionally allow historical internal data. Seed bypasses the
        # API serializer, so verify the capture against the current provider before
        # opening the transaction that removes any existing demo activities.
        from apps.academics.provider_catalog import catalog, resolve_selection
        from apps.exams.exceptions import Quiz24Error
        from rest_framework.exceptions import APIException
        try:
            catalog('grades', force_refresh=True)
            catalog('courses', force_refresh=True)
            for course in {selection['course'] for selection in self.selections}:
                catalog('course_detail', grade_id=grade, course_id=course, force_refresh=True)
            self.selections = [resolve_selection(selection) for selection in self.selections]
            if any(selection['labels']['grade'] != 'دوازدهم تجربی' for selection in self.selections):
                raise CommandError('Captured grade does not match the canonical demo profile.')
        except (Quiz24Error, APIException) as error:
            raise CommandError('Live Quiz24 validation failed; existing demo data was not changed.') from error


    def _create_activity(self, profiles, history):
        from apps.daily_reports.services import report_today
        today = report_today()
        first = today - timedelta(days=history - 1)
        final = saturday(today) + timedelta(days=6)
        statuses = [DailyReportItem.ReportedStatus.COMPLETED, DailyReportItem.ReportedStatus.PARTIAL,
                    DailyReportItem.ReportedStatus.NOT_DONE, None]
        for student in profiles:
            cursor = saturday(first)
            while cursor <= today:
                plan = Plan.objects.create(student=student, counselor=student.counselor,
                    title=f'برنامه هفته {cursor}', start_date=cursor, end_date=cursor + timedelta(days=6),
                    status=Plan.Status.PUBLISHED, published_at=timezone.now())
                for offset in range(7):
                    date = cursor + timedelta(days=offset)
                    if not first <= date <= final:
                        continue
                    day = PlanDay.objects.create(plan=plan, date=date)
                    report = DailyReport.objects.create(student=student, date=date) if date <= today else None
                    for order, selection in enumerate(self.selections):
                        item = PlanItem.objects.create(plan_day=day, kind=PlanItem.Kind.STUDY,
                            ordering=order, taxonomy=selection, title=build_title(selection),
                            title_is_custom=False, planned_duration_minutes=60)
                        if report:
                            status = statuses[order % len(statuses)]
                            DailyReportItem.objects.create(report=report, plan_item=item,
                                reported_status=status, ordering=order,
                                duration_source=DailyReportItem.DurationSource.MANUAL if status in statuses[:2] else "",
                                actual_duration_minutes=60 if status == statuses[0] else 30 if status == statuses[1] else None)
                            if status:
                                PlanItemExecution.objects.create(student=student, plan_item=item, status=status)
                cursor += timedelta(days=7)

    def _counts(self, student_names):
        plans = Plan.objects.filter(student__user__username__in=student_names)
        reports = DailyReport.objects.filter(student__user__username__in=student_names)
        return {
            "Counselors": len(self._demo_names()["counselors"]),
            "Students": StudentProfile.objects.filter(user__username__in=student_names).count(),
            "Published plans": plans.filter(status=Plan.Status.PUBLISHED).count(),
            "Draft plans": plans.filter(status=Plan.Status.DRAFT).count(),
            "Plan items": PlanItem.objects.filter(plan_day__plan__in=plans).count(),
            "Daily reports": reports.count(),
            "Report items": DailyReportItem.objects.filter(report__in=reports).count(),
            "Execution records": PlanItemExecution.objects.filter(student__user__username__in=student_names).count(),
        }
