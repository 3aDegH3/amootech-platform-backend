"""Guarded, deterministic development data from the existing product models."""

import io
import os
import random
import re
from datetime import datetime, time, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.academics.models import Chapter, Subject
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
        with transaction.atomic():
            call_command("seed_academics", stdout=io.StringIO())
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
        subjects = list(Subject.objects.filter(is_active=True, field__is_active=True,
            field__grade__is_active=True).select_related("field__grade")
            .prefetch_related(Prefetch("chapters", queryset=Chapter.objects.filter(is_active=True)
                .prefetch_related("topics"))))
        by_field = {}
        for subject in subjects:
            by_field.setdefault(subject.field_id, []).append(subject)
        fields = [items[0].field for items in by_field.values() if len(items) >= 3]
        if not fields:
            raise CommandError("No usable academic fields exist after seed_academics.")
        self.field_subjects = by_field
        profiles = []
        for index in range(1, count * per + 1):
            first = FIRST_NAMES[(index - 1) % len(FIRST_NAMES)]
            last = LAST_NAMES[(index - 1 + (index - 1) // len(FIRST_NAMES)) % len(LAST_NAMES)]
            user = self._user(student_name(index), User.Role.STUDENT, first, last, f"student{index:03d}@demo.local")
            field = fields[(index - 1) % len(fields)]
            profile, _ = StudentProfile.objects.update_or_create(user=user, defaults={
                "counselor": counselors[(index - 1) // per], "grade": field.grade, "field": field,
                "school_name": "دبیرستان نمونه آموتک",
            })
            profiles.append(profile)
        return profiles

    def _create_activity(self, profiles, history):
        today = timezone.localdate()
        first = today - timedelta(days=history - 1)
        final = saturday(today) + timedelta(days=6)
        published_at = timezone.now()
        for index, student in enumerate(profiles, 1):
            rng = random.Random(17031 + index)
            # The fourth pattern has more tests; completion probabilities below
            # vary across high, medium, and low adherence without product labels.
            subjects = self.field_subjects[student.field_id]
            focus = subjects[4:9] if len(subjects) >= 8 else subjects
            cursor, starts = saturday(first), []
            while cursor <= today:
                starts.append(cursor)
                cursor += timedelta(days=7)
            plans = Plan.objects.bulk_create([
                Plan(student=student, counselor=student.counselor, title=f"برنامه هفته {start.isoformat()}",
                    start_date=start, end_date=start + timedelta(days=6), status=Plan.Status.PUBLISHED,
                    published_at=published_at) for start in starts
            ])
            days = PlanDay.objects.bulk_create([
                PlanDay(plan=plan, date=plan.start_date + timedelta(days=offset))
                for plan in plans for offset in range(7)
                if first <= plan.start_date + timedelta(days=offset) <= final
            ])
            items = []
            for day in days:
                kinds = ["STUDY", "STUDY", "TEST", "REVIEW"]
                if index % 4 == 0:
                    kinds.append("TEST")
                elif rng.random() < .75:
                    kinds.append(rng.choice(("STUDY", "TEST", "EXAM")))
                if rng.random() < .40:
                    kinds.append("EVENT")
                for order, kind in enumerate(kinds):
                    if kind == "EVENT":
                        items.append(PlanItem(plan_day=day, kind=kind, ordering=order,
                            title=rng.choice(EVENTS), start_time=time(17), end_time=time(18, 30)))
                        continue
                    subject = rng.choices(focus, weights=[max(1, 7 - pos) for pos in range(len(focus))])[0]
                    chapters = list(subject.chapters.all())
                    chapter = rng.choice(chapters) if chapters else None
                    topics = list(chapter.topics.all()) if chapter else []
                    topic = topics[0] if topics and rng.random() < .65 else rng.choice(topics) if topics else None
                    duration = rng.choice((45, 60, 75, 90, 120))
                    hour = 8 + order * 2
                    items.append(PlanItem(plan_day=day, kind=kind, ordering=order,
                        title=f"آزمون {subject.name}" if kind == "EXAM" else "",
                        subject=subject, chapter=chapter, topic=topic,
                        planned_duration_minutes=duration,
                        test_count=rng.choice((25, 30, 40, 40) if index % 4 == 0 else (10, 15, 20, 25, 30, 40)) if kind == "TEST" else None,
                        start_time=time(hour) if order < 2 else None,
                        end_time=time(hour + duration // 60, duration % 60) if order < 2 else None))
            items = PlanItem.objects.bulk_create(items)
            by_date = {}
            for item in items:
                by_date.setdefault(item.plan_day.date, []).append(item)
            if index % 5 == 0:
                start = saturday(today) + timedelta(days=7)
                draft = Plan.objects.create(student=student, counselor=student.counselor,
                    title="پیش‌نویس هفته بعد", start_date=start, end_date=start + timedelta(days=6))
                draft_day = PlanDay.objects.create(plan=draft, date=start)
                PlanItem.objects.create(plan_day=draft_day, kind="STUDY", subject=focus[0], planned_duration_minutes=90)
            reports = DailyReport.objects.bulk_create([
                DailyReport(student=student, date=date,
                    wake_time=time(rng.choice((6, 7, 8)), rng.choice((0, 15, 30))),
                    sleep_time=time(rng.choice((22, 23)), rng.choice((0, 15, 30))),
                    mobile_minutes=rng.randrange(35, 225, 5), self_rating=rng.randint(11, 19))
                for date in (first + timedelta(days=offset) for offset in range(history))
                if rng.random() < (.62 if date == today else .78)
            ])
            report_by_date = {report.date: report for report in reports}
            executions, rows = [], []
            completed_chance, partial_chance = ((.92, .08), (.72, .16), (.48, .22), (.80, .12))[(index - 1) % 4]
            for date, day_items in by_date.items():
                if date > today:
                    continue
                report = report_by_date.get(date)
                for item in day_items:
                    if item.kind == "EVENT":
                        if report:
                            rows.append(DailyReportItem(report=report, plan_item=item, ordering=item.ordering))
                        continue
                    roll = rng.random()
                    status = "COMPLETED" if roll < completed_chance else "PARTIAL" if roll < completed_chance + partial_chance else None
                    actual, timer = None, False
                    if status:
                        factor = rng.uniform(.82, 1.12) if status == "COMPLETED" else rng.uniform(.25, .68)
                        actual = max(10, round(item.planned_duration_minutes * factor))
                        timer = rng.random() < .55
                        started = timezone.make_aware(datetime.combine(date, time(8))) if timer else None
                        executions.append(PlanItemExecution(student=student, plan_item=item, status=status,
                            started_at=started, accumulated_seconds=actual * 60 if timer else 0,
                            completed_at=timezone.make_aware(datetime.combine(date, time(19)))))
                    if not report:
                        continue
                    row = DailyReportItem(report=report, plan_item=item, ordering=item.ordering)
                    if status:
                        if not timer or rng.random() < .10:
                            row.actual_duration_minutes = max(0, actual + rng.choice((-5, 0, 5))) if timer else actual
                            row.duration_source = DailyReportItem.DurationSource.MANUAL
                        if item.kind == "TEST":
                            count = max(1, round(item.test_count * (rng.uniform(.82, 1.06) if status == "COMPLETED" else rng.uniform(.35, .72))))
                            wrong = rng.randint(0, max(1, count // 4))
                            unanswered = rng.randint(0, max(0, count // 8))
                            row.actual_test_count = count
                            row.wrong_count, row.unanswered_count = wrong, unanswered
                            row.correct_count = count - wrong - unanswered
                    rows.append(row)
                if report and rng.random() < .28:
                    subject = rng.choice(focus)
                    chapter = rng.choice(list(subject.chapters.all()))
                    topics = list(chapter.topics.all())
                    kind = "TEST" if index % 4 == 0 else rng.choice(("STUDY", "REVIEW", "TEST"))
                    extra = DailyReportItem(report=report, kind=kind, subject=subject, chapter=chapter,
                        topic=rng.choice(topics) if topics else None,
                        actual_duration_minutes=rng.choice((25, 30, 40, 45, 60)),
                        duration_source=DailyReportItem.DurationSource.MANUAL,
                        resource=rng.choice(RESOURCES), ordering=20)
                    if kind == "TEST":
                        count = rng.choice((10, 15, 20))
                        wrong = rng.randint(0, count // 4)
                        extra.actual_test_count, extra.wrong_count = count, wrong
                        extra.correct_count, extra.unanswered_count = count - wrong, 0
                    rows.append(extra)
                if report and rng.random() < .12:
                    rows.append(DailyReportItem(report=report, kind="EVENT", title=rng.choice(EVENTS),
                        start_time=time(17), end_time=time(18), actual_duration_minutes=60,
                        duration_source=DailyReportItem.DurationSource.MANUAL, ordering=21))
            PlanItemExecution.objects.bulk_create(executions)
            DailyReportItem.objects.bulk_create(rows)

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
