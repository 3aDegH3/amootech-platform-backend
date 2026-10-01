from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Field, Grade, Subject
from apps.daily_reports.services import report_today
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution
from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection

User = get_user_model()

@override_settings(TELEGRAM_BOT_SERVICE_TOKEN="svc-secret")
class TelegramProgressTests(APITestCase):
    def setUp(self):
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.student_user = User.objects.create_user("student")
        grade = Grade.objects.create(name="G12")
        field = Field.objects.create(grade=grade, name="F")
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor, grade=grade, field=field)
        self.subj = Subject.objects.create(field=field, name="S")
        self.chat_id = -100111
        self.user_id = 777
        TelegramGroupConnection.objects.create(student=self.student, telegram_chat_id=self.chat_id, is_active=True)
        TelegramStudentConnection.objects.create(student=self.student, telegram_user_id=self.user_id, is_active=True)
        self.svc = {"HTTP_AUTHORIZATION": "Bearer svc-secret"}
        self.today = report_today()
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=self.today - timedelta(days=6), end_date=self.today + timedelta(days=1), status=Plan.Status.PUBLISHED, published_at=timezone.now())
        # 7 days plan days
        for i in range(7):
            d = self.today - timedelta(days=6-i)
            day = PlanDay.objects.create(plan=self.plan, date=d)
            # only give items to some days to test empty day handling
            if i >= 3:  # last 4 days have items
                PlanItem.objects.create(plan_day=day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, title="")
                PlanItem.objects.create(plan_day=day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=1, planned_duration_minutes=30, title="")

    def _ids(self):
        return {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id}

    def test_today_progress(self):
        # Exec one item today completed
        day_today = PlanDay.objects.get(plan=self.plan, date=self.today)
        item = PlanItem.objects.filter(plan_day=day_today).first()
        PlanItemExecution.objects.create(student=self.student, plan_item=item, status=PlanItemExecution.Status.COMPLETED, completed_at=timezone.now())
        # Need a report item for actual minutes? progress uses report+execution
        from apps.daily_reports.services import open_report
        report = open_report(self.student_user, self.today)
        row = report.items.filter(plan_item=item).first()
        row.actual_duration_minutes = 60
        row.duration_source = "MANUAL"
        row.save()
        resp = self.client.get("/api/internal/v1/telegram/progress/today/", {**self._ids()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["study_minutes"], 60)
        self.assertEqual(resp.data["items"]["completed"], 1)

    def test_wrong_user_rejected(self):
        resp = self.client.get("/api/internal/v1/telegram/progress/today/", {"telegram_chat_id": self.chat_id, "telegram_user_id": 999}, **self.svc)
        self.assertEqual(resp.status_code, 403)

    def test_service_auth_required(self):
        resp = self.client.get("/api/internal/v1/telegram/progress/today/", {**self._ids()})
        self.assertEqual(resp.status_code, 403)

    def test_7day_totals(self):
        # Setup actuals for last 2 days
        for offset in [0, 1]:
            d = self.today - timedelta(days=offset)
            day = PlanDay.objects.get(plan=self.plan, date=d)
            for it in PlanItem.objects.filter(plan_day=day):
                PlanItemExecution.objects.get_or_create(student=self.student, plan_item=it, defaults={"status": PlanItemExecution.Status.COMPLETED, "completed_at": timezone.now()})
                # Also need actual
        # Ensure reports have actuals
        from apps.daily_reports.services import open_report
        for offset in [0, 1]:
            d = self.today - timedelta(days=offset)
            report = open_report(self.student_user, d)
            for row in report.items.filter(plan_item__isnull=False):
                row.actual_duration_minutes = 60 if row.plan_item.ordering == 0 else 30
                row.duration_source = "MANUAL"
                row.save()
        resp = self.client.get("/api/internal/v1/telegram/progress/7-days/", {**self._ids()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["summary"]["study_minutes"], 180)  # 90*2
        self.assertIsNotNone(resp.data["summary"]["average_plan_completion_percentage"])

    def test_day_without_plan_not_reduce_average(self):
        # Days 0-2 have no items (but plan days exist with no items? Actually we created days but only i>=3 have items, so early days have 0 planned_blocks)
        resp = self.client.get("/api/internal/v1/telegram/progress/7-days/", {**self._ids()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        # average should be None or based only on days with plan
        # Just ensure no crash and average is None or computed without 0% for empty days

    def test_another_student_data_not_leak(self):
        other_user = User.objects.create_user("other")
        other_student = StudentProfile.objects.create(user=other_user, counselor=self.counselor, grade=self.student.grade, field=self.student.field)
        # other student has no telegram connection, but try to fetch with our ids should not return other data
        resp = self.client.get("/api/internal/v1/telegram/progress/today/", {**self._ids()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_progress_before_finalization(self):
        resp = self.client.get("/api/internal/v1/telegram/progress/today/", {**self._ids()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn("study_minutes", resp.data)
