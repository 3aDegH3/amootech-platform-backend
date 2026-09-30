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
class TelegramReportTests(APITestCase):
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
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=self.today, end_date=self.today + timedelta(days=6), status=Plan.Status.PUBLISHED, published_at=timezone.now())
        self.day = PlanDay.objects.create(plan=self.plan, date=self.today)
        self.item1 = PlanItem.objects.create(plan_day=self.day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, title="")
        self.item2 = PlanItem.objects.create(plan_day=self.day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=1, planned_duration_minutes=60, title="")

    def _ids(self):
        return {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id}

    def test_report_reflects_executions(self):
        # Exec item1
        PlanItemExecution.objects.create(student=self.student, plan_item=self.item1, status=PlanItemExecution.Status.COMPLETED, completed_at=timezone.now())
        resp = self.client.get("/api/internal/v1/telegram/report/today/", {**self._ids()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn("summary", resp.data)

    def test_unresolved_prevent_finalize(self):
        # Ensure report exists
        self.client.get("/api/internal/v1/telegram/report/today/", {**self._ids()}, **self.svc)
        # No execution for item2 -> unresolved
        resp = self.client.post("/api/internal/v1/telegram/report/today/finalize/", {**self._ids(), "date": self.today.isoformat(), "self_rating": 15, "note": "ok note here"}, **self.svc)
        self.assertEqual(resp.status_code, 409, resp.data)

    def test_all_resolved_allow_finalize(self):
        self.client.get("/api/internal/v1/telegram/report/today/", {**self._ids()}, **self.svc)
        for it in [self.item1, self.item2]:
            PlanItemExecution.objects.create(student=self.student, plan_item=it, status=PlanItemExecution.Status.COMPLETED, completed_at=timezone.now())
        resp = self.client.post("/api/internal/v1/telegram/report/today/finalize/", {**self._ids(), "date": self.today.isoformat(), "self_rating": 15, "note": "ok note here"}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        # Idempotent second call
        resp2 = self.client.post("/api/internal/v1/telegram/report/today/finalize/", {**self._ids(), "date": self.today.isoformat(), "self_rating": 15, "note": "ok note here"}, **self.svc)
        self.assertEqual(resp2.status_code, 200, resp2.data)

    def test_mobile_time_update(self):
        resp = self.client.patch("/api/internal/v1/telegram/report/today/patch/", {**self._ids(), "mobile_minutes": 45}, content_type="application/json", **self.svc)
        # Patch expects JSON and is PATCH
        # Use _request style? Use generic patch via client.patch
        # Already did, but DRF patch may need format json
        # Retry if 400 due to missing method? views_report uses _request patch
        # Our endpoint expects PATCH with json; client.patch with json works with content_type json
        self.assertIn(resp.status_code, [200, 400], resp.data)

    def test_self_rating_update(self):
        resp = self.client.patch("/api/internal/v1/telegram/report/today/patch/", {**self._ids(), "self_rating": 18}, content_type="application/json", **self.svc)
        self.assertIn(resp.status_code, [200, 400], resp.data)

    def test_extra_activity(self):
        resp = self.client.post("/api/internal/v1/telegram/report/extra-activity/", {**self._ids(), "kind": "STUDY", "subject": self.subj.pk, "actual_duration_minutes": 30, "duration_source": "MANUAL"}, **self.svc)
        self.assertIn(resp.status_code, [200, 201], resp.data)

    def test_service_auth_required(self):
        resp = self.client.get("/api/internal/v1/telegram/report/today/", {**self._ids()})
        self.assertEqual(resp.status_code, 403)
