from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Chapter, Field, Grade, Subject
from apps.daily_reports.services import report_today
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution
from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection

User = get_user_model()


@override_settings(TELEGRAM_BOT_SERVICE_TOKEN="svc-secret")
class TelegramExecutionTests(APITestCase):
    def setUp(self):
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.student_user = User.objects.create_user("student")
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        self.other_user = User.objects.create_user("other")
        self.other_student = StudentProfile.objects.create(user=self.other_user, counselor=self.counselor)
        grade = Grade.objects.create(name="G12")
        field = Field.objects.create(grade=grade, name="F")
        self.subj = Subject.objects.create(field=field, name="S")
        self.chat_id = -100111
        self.user_id = 777
        TelegramGroupConnection.objects.create(student=self.student, telegram_chat_id=self.chat_id, is_active=True)
        TelegramStudentConnection.objects.create(student=self.student, telegram_user_id=self.user_id, is_active=True)
        self.svc = {"HTTP_AUTHORIZATION": "Bearer svc-secret"}
        self.today = report_today()
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=self.today, end_date=self.today + timedelta(days=6), status=Plan.Status.PUBLISHED, published_at=timezone.now())
        self.day = PlanDay.objects.create(plan=self.plan, date=self.today)
        self.item = PlanItem.objects.create(plan_day=self.day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, title="")
        self.item_test = PlanItem.objects.create(plan_day=self.day, kind=PlanItem.Kind.TEST, subject=self.subj, ordering=1, planned_duration_minutes=60, test_count=30, title="")

    def _identity(self):
        return {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id}

    def test_can_update_own_item(self):
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item.pk, "status": "COMPLETED"}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(PlanItemExecution.objects.filter(plan_item=self.item, status=PlanItemExecution.Status.COMPLETED).exists())

    def test_cannot_update_other_students_item(self):
        # Create other plan
        plan2 = Plan.objects.create(student=self.other_student, counselor=self.counselor, start_date=self.today, end_date=self.today + timedelta(days=6), status=Plan.Status.PUBLISHED, published_at=timezone.now())
        day2 = PlanDay.objects.create(plan=plan2, date=self.today)
        item2 = PlanItem.objects.create(plan_day=day2, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, title="")
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": item2.pk, "status": "COMPLETED"}, **self.svc)
        self.assertEqual(resp.status_code, 404, resp.data)

    def test_completed_with_planned_values(self):
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item.pk, "status": "COMPLETED", "actual_duration_minutes": 60, "duration_source": "PLAN"}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_partial_flow(self):
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item.pk, "status": "PARTIAL", "actual_duration_minutes": 30, "duration_source": "MANUAL"}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(PlanItemExecution.objects.get(plan_item=self.item).status, PlanItemExecution.Status.PARTIAL)

    def test_not_completed(self):
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item.pk, "status": "NOT_DONE"}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_existing_updated_not_duplicated(self):
        self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item.pk, "status": "COMPLETED"}, **self.svc)
        self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item.pk, "status": "PARTIAL"}, **self.svc)
        self.assertEqual(PlanItemExecution.objects.filter(plan_item=self.item).count(), 1)
        self.assertEqual(PlanItemExecution.objects.get(plan_item=self.item).status, PlanItemExecution.Status.PARTIAL)

    def test_breakdown_validation(self):
        # Invalid breakdown: sum > total
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item_test.pk, "status": "COMPLETED", "actual_duration_minutes": 60, "duration_source": "MANUAL", "actual_test_count": 30, "correct_count": 20, "wrong_count": 20, "unanswered_count": 0}, **self.svc)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_draft_cannot_be_executed(self):
        self.plan.status = Plan.Status.DRAFT
        self.plan.published_at = None
        self.plan.save()
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item.pk, "status": "COMPLETED"}, **self.svc)
        self.assertIn(resp.status_code, [404, 409], resp.data)

    def test_service_auth_required(self):
        resp = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {**self._identity(), "plan_item_id": self.item.pk, "status": "COMPLETED"})
        self.assertEqual(resp.status_code, 403)
