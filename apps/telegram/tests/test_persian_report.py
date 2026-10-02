"""Tests for report-by-date 7-day window, finalized immutability, execution date validation."""
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
from apps.daily_reports.models import DailyReport

User = get_user_model()

@override_settings(TELEGRAM_BOT_SERVICE_TOKEN="svc-secret")
class PersianReportDayTests(APITestCase):
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
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=self.today - timedelta(days=7), end_date=self.today + timedelta(days=6), status=Plan.Status.PUBLISHED, published_at=timezone.now())
        for i in range(8):
            d = self.today - timedelta(days=i)
            day = PlanDay.objects.create(plan=self.plan, date=d)
            PlanItem.objects.create(plan_day=day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, title="")

    def _ids(self, extra=None):
        base = {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id}
        if extra:
            base.update(extra)
        return base

    def test_report_day_accepts_today(self):
        resp = self.client.get("/api/internal/v1/telegram/report/day/", {**self._ids(), "date": self.today.isoformat()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_report_day_accepts_yesterday(self):
        resp = self.client.get("/api/internal/v1/telegram/report/day/", {**self._ids(), "date": (self.today - timedelta(days=1)).isoformat()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_report_up_to_7_days_accepted(self):
        resp = self.client.get("/api/internal/v1/telegram/report/day/", {**self._ids(), "date": (self.today - timedelta(days=7)).isoformat()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_older_than_7_rejected(self):
        resp = self.client.get("/api/internal/v1/telegram/report/day/", {**self._ids(), "date": (self.today - timedelta(days=8)).isoformat()}, **self.svc)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["code"], "out_of_range")

    def test_future_rejected(self):
        resp = self.client.get("/api/internal/v1/telegram/report/day/", {**self._ids(), "date": (self.today + timedelta(days=1)).isoformat()}, **self.svc)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["code"], "future_date")

    def test_another_student_cannot_access(self):
        other_user = User.objects.create_user("other")
        other_student = StudentProfile.objects.create(user=other_user, counselor=self.counselor)
        # Try to use other student's plan item via execution
        other_day = PlanDay.objects.filter(plan=self.plan).first()
        other_item = PlanItem.objects.filter(plan_day=other_day).first()
        # This item belongs to self.student, so other student's connection would fail identity check
        # Instead test service auth + ownership: create separate student connection and try wrong ids
        # The _resolve_student will still return self.student for our chat_id/user_id, so we test execution ownership via plan_item
        # Create a plan for other student
        plan2 = Plan.objects.create(student=other_student, counselor=self.counselor, start_date=self.today, end_date=self.today, status=Plan.Status.PUBLISHED, published_at=timezone.now())
        day2 = PlanDay.objects.create(plan=plan2, date=self.today)
        item2 = PlanItem.objects.create(plan_day=day2, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, title="")
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._ids(), "plan_item_id": item2.pk, "status": "COMPLETED"}, **self.svc)
        self.assertEqual(resp.status_code, 404, resp.data)

    def test_finalized_past_report_respects_immutability(self):
        past = self.today - timedelta(days=2)
        report = DailyReport.objects.create(student=self.student, date=past, closed_at=timezone.now(), note="done", self_rating=15)
        # Try to patch finalized past report
        resp = self.client.patch("/api/internal/v1/telegram/report/today/patch/", {**self._ids(), "date": past.isoformat(), "mobile_minutes": 10}, content_type="application/json", **self.svc)
        self.assertEqual(resp.status_code, 409, resp.data)
        # Try to execute on finalized day
        past_day = PlanDay.objects.get(plan=self.plan, date=past)
        past_item = PlanItem.objects.filter(plan_day=past_day).first()
        resp2 = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._ids(), "plan_item_id": past_item.pk, "status": "COMPLETED"}, **self.svc)
        self.assertEqual(resp2.status_code, 409, resp2.data)

    def test_past_execution_updates_not_duplicated(self):
        past = self.today - timedelta(days=1)
        past_day = PlanDay.objects.get(plan=self.plan, date=past)
        past_item = PlanItem.objects.filter(plan_day=past_day).first()
        # First execution
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._ids(), "plan_item_id": past_item.pk, "status": "COMPLETED"}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        # Second should update not duplicate
        resp2 = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._ids(), "plan_item_id": past_item.pk, "status": "PARTIAL"}, **self.svc)
        self.assertEqual(resp2.status_code, 200, resp2.data)
        self.assertEqual(PlanItemExecution.objects.filter(plan_item=past_item).count(), 1)
        self.assertEqual(PlanItemExecution.objects.get(plan_item=past_item).status, PlanItemExecution.Status.PARTIAL)

    def test_service_auth_required(self):
        resp = self.client.get("/api/internal/v1/telegram/report/day/", {**self._ids(), "date": self.today.isoformat()})
        self.assertEqual(resp.status_code, 403)

    def test_execution_future_rejected(self):
        future = self.today + timedelta(days=1)
        # Create future plan day
        future_day = PlanDay.objects.create(plan=self.plan, date=future) if not PlanDay.objects.filter(plan=self.plan, date=future).exists() else PlanDay.objects.get(plan=self.plan, date=future)
        # Need plan covering future — extend
        if future_day.plan_id != self.plan.pk:
            pass
        # Create an item for future
        item = PlanItem.objects.create(plan_day=future_day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=99, planned_duration_minutes=60, title="")
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._ids(), "plan_item_id": item.pk, "status": "COMPLETED"}, **self.svc)
        # Should be rejected as future
        self.assertIn(resp.status_code, [400, 409], resp.data)

    def test_execution_beyond_7_days_rejected(self):
        old = self.today - timedelta(days=8)
        # Create old day outside plan range — need separate plan
        old_plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=old, end_date=old, status=Plan.Status.PUBLISHED, published_at=timezone.now())
        old_day = PlanDay.objects.create(plan=old_plan, date=old)
        item = PlanItem.objects.create(plan_day=old_day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, title="")
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._ids(), "plan_item_id": item.pk, "status": "COMPLETED"}, **self.svc)
        self.assertEqual(resp.status_code, 400, resp.data)
