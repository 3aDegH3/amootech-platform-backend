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
        parser.add_argument("--reset", action="store_true",
                            help="Rebuild demo plans/reports while preserving demo accounts and exam references.")

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
            # Existing Exam.counselor uses PROTECT. Never delete demo accounts:
            # keep their primary keys and historical exam relationships intact.
            # Both modes rebuild demo plans/reports, as the previous command did.
            self._clear_transactions(existing["students"])
            profiles = self._ensure_people(counselor_count, per)
            # Older demo accounts outside the requested count remain untouched.
            # Deleting accounts needs a separate, explicitly reviewed cleanup.
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
        academic_fields = []
        for number, name in ((10,'دهم'),(11,'یازدهم'),(12,'دوازدهم')):
            grade, _ = Grade.objects.get_or_create(number=number, defaults={'name':name,'ordering':number})
            for field_name in ('تجربی', 'ریاضی'):
                field, _ = Field.objects.get_or_create(grade=grade,name=field_name)
                if grade.name != name or not grade.is_active or not field.is_active:
                    raise CommandError('Canonical grade/field is unavailable; existing data was not changed.')
                academic_fields.append(field)
        profiles = []
        for index in range(1, count * per + 1):
            first = FIRST_NAMES[(index - 1) % len(FIRST_NAMES)]
            last = LAST_NAMES[(index - 1 + (index - 1) // len(FIRST_NAMES)) % len(LAST_NAMES)]
            user = self._user(student_name(index), User.Role.STUDENT, first, last, f"student{index:03d}@demo.local")
            field = academic_fields[(index - 1) % len(academic_fields)]
            profile, _ = StudentProfile.objects.update_or_create(user=user, defaults={
                "counselor": counselors[(index - 1) // per], "grade": field.grade, "field": field,
                "school_name": "دبیرستان نمونه آموتک",
            })
            profiles.append(profile)
        return profiles

    def _load_catalog(self):
        """Use only the verified local catalog; no live requests or invented IDs."""
        from apps.academics.provider_catalog import catalog, rows, field, resolve_selection
        try:
            grades = {int(field(row,'gradeid','id','key')): row for row in rows(catalog('grades'))
                      if field(row,'canonicalgradenumber') in (10,11,12)
                      and field(row,'verifiedfieldname') in ('ریاضی','تجربی')}
            self.selections_by_course = {}
            for course in rows(catalog('courses')):
                grade = int(field(course,'gradeid'))
                if grade not in grades:
                    continue
                identifier = int(field(course,'courseid','id','key'))
                detail = catalog('course_detail',grade_id=grade,course_id=identifier)
                index = taxonomy_index(detail, identifier, grade)
                deepest = next((index[level] for level in ('object','subject','chapter') if index[level]),{})
                candidates = [resolve_selection({'provider':'quiz24','grade':grade,**path}) for path in list(deepest.values())[:8]]
                if candidates:
                    self.selections_by_course[(grade,identifier)] = candidates
            if not self.selections_by_course:
                raise ValueError('No verified upper-secondary taxonomy.')
            self.selections = [selection for pool in self.selections_by_course.values() for selection in pool]
        except Exception as error:
            raise CommandError('Local Quiz24 catalog unavailable; run sync_quiz24_catalog before seeding. Existing data was not changed.') from error

    def _daily_selections(self, student_index, day_index, student):
        pools = [pool for key,pool in sorted(self.selections_by_course.items())
                 if pool[0]['labels']['grade'].endswith(' ' + student.field.name)]
        if not pools:
            raise CommandError('Verified catalog has no courses for the demo field.')
        selected = []
        for offset in range(min(3,len(pools))):
            pool = pools[(student_index + day_index + offset) % len(pools)]
            selected.append(pool[(student_index + day_index) % len(pool)])
        return selected

    def _create_activity(self, profiles, history):
        from apps.daily_reports.services import report_today

        today = report_today()
        first = today - timedelta(days=history - 1)
        final = saturday(today) + timedelta(days=6)
        statuses = (
            DailyReportItem.ReportedStatus.COMPLETED,
            DailyReportItem.ReportedStatus.PARTIAL,
            DailyReportItem.ReportedStatus.NOT_DONE,
            None,
        )
        durations = (45, 60, 75, 90, 50)
        from apps.planning.models import StudentFixedCommitment, StudentFixedEvent
        from datetime import time
        for student_index, student in enumerate(profiles):
            StudentFixedCommitment.objects.update_or_create(student=student,kind='SCHOOL',weekday=5,title='مدرسه نمایشی شنبه',defaults={'start_time':time(7,30),'end_time':time(13,30),'active':True})
            StudentFixedEvent.objects.update_or_create(student=student,title='امتحان نمایشی',defaults={'kind':'EXAM','date':today+timedelta(days=1),'weekday':None,'active':True})
            StudentFixedEvent.objects.update_or_create(student=student,title='کوییز هفتگی نمایشی',defaults={'kind':'QUIZ','date':None,'weekday':5,'valid_from':first,'valid_until':final,'active':True})
            cursor = saturday(first)
            while cursor <= today:
                plan = Plan.objects.create(
                    student=student,
                    counselor=student.counselor,
                    title='برنامه هفتگی نمایشی',
                    start_date=cursor,
                    end_date=cursor + timedelta(days=6),
                    status=Plan.Status.PUBLISHED,
                    published_at=timezone.now(),
                )
                for offset in range(7):
                    date = cursor + timedelta(days=offset)
                    if not first <= date <= final:
                        continue
                    day = PlanDay.objects.create(plan=plan, date=date)
                    if date.weekday() == 5:
                        PlanItem.objects.create(plan_day=day,kind=PlanItem.Kind.EVENT,title='مدرسه شنبه',start_time=time(7,30),end_time=time(13,30),planned_duration_minutes=360)
                    report = DailyReport.objects.create(student=student, date=date) if date <= today else None
                    day_index = (date - first).days
                    selections = self._daily_selections(student_index, day_index, student)
                    for order, selection in enumerate(selections):
                        selection = dict(selection)
                        # Exercise every valid stopping level without fabricating descendants.
                        depth = ('grade','course','chapter','subject','object')[(day_index + order) % 5]
                        level_order = ('grade','course','chapter','subject','object')
                        keep = set(level_order[:level_order.index(depth)+1])
                        selection = {key:value for key,value in selection.items() if key in keep or key == 'provider'}
                        from apps.academics.provider_catalog import resolve_selection
                        selection = resolve_selection(selection)
                        planned_minutes = durations[(day_index + student_index + order) % len(durations)]
                        item = PlanItem.objects.create(
                            plan_day=day,
                            kind=PlanItem.Kind.STUDY,
                            ordering=order,
                            taxonomy=selection,
                            title='مرور و تست سفارشی' if (day_index + order) % 7 == 0 else build_title(selection),
                            title_is_custom=(day_index + order) % 7 == 0,
                            planned_duration_minutes=planned_minutes,
                        )
                        if report is None:
                            continue
                        # At least one completed/partial topic each day, for report-based exam testing.
                        status = (statuses[(day_index + student_index + order) % len(statuses)]
                                  if order else statuses[day_index % 2])
                        has_duration = status in statuses[:2]
                        actual_minutes = (planned_minutes if status == statuses[0]
                                          else max(15, planned_minutes // 2) if status == statuses[1]
                                          else None)
                        DailyReportItem.objects.create(
                            report=report,
                            plan_item=item,
                            reported_status=status,
                            ordering=order,
                            duration_source=(DailyReportItem.DurationSource.MANUAL if has_duration else ''),
                            actual_duration_minutes=actual_minutes,
                        )
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
