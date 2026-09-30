from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Chapter, Field, Grade, Subject
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution
from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection

User = get_user_model()
REPORT_ZONE = ZoneInfo("Asia/Tehran")


def _today():
    from apps.daily_reports.services import report_today
    return report_today()


@override_settings(TELEGRAM_BOT_SERVICE_TOKEN="svc-secret")
class TelegramPlanApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", role=User.Role.ADMIN)
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.student_user = User.objects.create_user("student")
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        grade = Grade.objects.create(name="G12")
        field = Field.objects.create(grade=grade, name="Math")
        self.subj = Subject.objects.create(field=field, name="MathSub")
        self.svc = {"HTTP_AUTHORIZATION": "Bearer svc-secret"}
        # Telegram connections
        self.chat_id = -100123456
        self.user_id = 777777
        TelegramGroupConnection.objects.create(student=self.student, telegram_chat_id=self.chat_id, chat_title="G", is_active=True)
        TelegramStudentConnection.objects.create(student=self.student, telegram_user_id=self.user_id, telegram_username="u", is_active=True)
        # Published plan covering today and tomorrow
        self.today = _today()
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=self.today, end_date=self.today + timedelta(days=6), status=Plan.Status.PUBLISHED, published_at=timezone.now())
        self.day_today = PlanDay.objects.create(plan=self.plan, date=self.today)
        self.day_tomorrow = PlanDay.objects.create(plan=self.plan, date=self.today + timedelta(days=1))
        # Items today
        self.item1 = PlanItem.objects.create(plan_day=self.day_today, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, test_count=None, title="")
        self.item2 = PlanItem.objects.create(plan_day=self.day_today, kind=PlanItem.Kind.TEST, subject=self.subj, ordering=1, planned_duration_minutes=45, test_count=30, title="")
        self.item3 = PlanItem.objects.create(plan_day=self.day_today, kind=PlanItem.Kind.REVIEW, subject=self.subj, ordering=2, planned_duration_minutes=30, title="")

    def _svc_params(self, chat=None, user=None):
        c = chat if chat is not None else self.chat_id
        u = user if user is not None else self.user_id
        return {"telegram_chat_id": str(c), "telegram_user_id": str(u)}

    def test_connected_gets_today_published(self):
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {**self._svc_params()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["has_plan"])
        self.assertEqual(len(resp.data["items"]), 3)

    def test_unconnected_rejected(self):
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {"telegram_chat_id": str(self.chat_id), "telegram_user_id": "999999"}, **self.svc)
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_wrong_user_for_group_rejected(self):
        # Different group not connected
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {"telegram_chat_id": "-999", "telegram_user_id": str(self.user_id)}, **self.svc)
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_unpublished_not_returned(self):
        # Make plan draft
        self.plan.status = Plan.Status.DRAFT
        self.plan.published_at = None
        self.plan.save()
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {**self._svc_params()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data["has_plan"])

    def test_no_plan_returns_has_plan_false(self):
        Plan.objects.all().delete()
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {**self._svc_params()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data["has_plan"])

    def test_execution_statuses_represented(self):
        # item1 completed, item2 partial, item3 not_done, leftover pending already covered
        PlanItemExecution.objects.create(student=self.student, plan_item=self.item1, status=PlanItemExecution.Status.COMPLETED, accumulated_seconds=3600, started_at=timezone.now())
        PlanItemExecution.objects.create(student=self.student, plan_item=self.item2, status=PlanItemExecution.Status.PARTIAL, accumulated_seconds=1800, started_at=timezone.now())
        PlanItemExecution.objects.create(student=self.student, plan_item=self.item3, status=PlanItemExecution.Status.NOT_DONE)
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {**self._svc_params()}, **self.svc)
        by_id = {i["id"]: i for i in resp.data["items"]}
        self.assertEqual(by_id[self.item1.pk]["execution_status"], "COMPLETED")
        self.assertEqual(by_id[self.item2.pk]["execution_status"], "PARTIAL")
        self.assertEqual(by_id[self.item3.pk]["execution_status"], "NOT_DONE")

    def test_summary_completion_calculation(self):
        PlanItemExecution.objects.create(student=self.student, plan_item=self.item1, status=PlanItemExecution.Status.COMPLETED, accumulated_seconds=3600, started_at=timezone.now())
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {**self._svc_params()}, **self.svc)
        s = resp.data["summary"]
        self.assertEqual(s["total_items"], 3)
        self.assertEqual(s["completed"], 1)
        self.assertEqual(s["completion_percentage"], 33)  # 1/3

    def test_tomorrow_returns_correct_day(self):
        resp = self.client.get("/api/internal/v1/telegram/plan/tomorrow/", {**self._svc_params()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["date"], (self.today + timedelta(days=1)).isoformat())

    def test_week_summary_correct(self):
        # Add items to tomorrow as well
        PlanItem.objects.create(plan_day=self.day_tomorrow, kind=PlanItem.Kind.TEST, subject=self.subj, ordering=0, planned_duration_minutes=120, test_count=20)
        resp = self.client.get("/api/internal/v1/telegram/plan/week/", {**self._svc_params()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(len(resp.data["days"]) >= 2)
        today_entry = next(d for d in resp.data["days"] if d["date"] == self.today.isoformat())
        self.assertEqual(today_entry["total_items"], 3)

    def test_pdf_returns_export(self):
        resp = self.client.get("/api/internal/v1/telegram/plan/current/pdf/", {**self._svc_params()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.content[:4])
        self.assertIn("application/pdf", resp["Content-Type"])

    def test_excel_returns_export(self):
        resp = self.client.get("/api/internal/v1/telegram/plan/current/excel/", {**self._svc_params()}, **self.svc)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sheet", resp["Content-Type"])

    def test_service_auth_required(self):
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {**self._svc_params()})
        self.assertEqual(resp.status_code, 403)

    def test_day_by_date(self):
        resp = self.client.get("/api/internal/v1/telegram/plan/day/", {**self._svc_params(), "date": self.today.isoformat()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["date"], self.today.isoformat())
