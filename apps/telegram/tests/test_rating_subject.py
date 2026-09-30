from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Field, Grade, Subject
from apps.daily_reports.services import report_today
from apps.daily_reports.models import DailyReport
from apps.planning.models import Plan, PlanDay, PlanItem
from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection

User = get_user_model()

@override_settings(TELEGRAM_BOT_SERVICE_TOKEN="svc-secret")
class RatingSubjectTests(APITestCase):
    def setUp(self):
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.student_user = User.objects.create_user("student")
        grade = Grade.objects.create(name="G12")
        field = Field.objects.create(grade=grade, name="F")
        other_grade = Grade.objects.create(name="G11")
        other_field = Field.objects.create(grade=other_grade, name="F_other")
        self.grade = grade
        self.field = field
        self.other_grade = other_grade
        self.other_field = other_field
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor, grade=grade, field=field)
        self.subj = Subject.objects.create(field=field, name="S1")
        self.subj2 = Subject.objects.create(field=field, name="S2")
        self.unrelated_subj = Subject.objects.create(field=other_field, name="Unrelated")
        self.chat_id = -100111
        self.user_id = 777
        TelegramGroupConnection.objects.create(student=self.student, telegram_chat_id=self.chat_id, is_active=True)
        TelegramStudentConnection.objects.create(student=self.student, telegram_user_id=self.user_id, is_active=True)
        self.svc = {"HTTP_AUTHORIZATION": "Bearer svc-secret"}
        self.today = report_today()
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=self.today, end_date=self.today + timedelta(days=6), status=Plan.Status.PUBLISHED, published_at=timezone.now())
        self.day = PlanDay.objects.create(plan=self.plan, date=self.today)
        self.item = PlanItem.objects.create(plan_day=self.day, kind=PlanItem.Kind.STUDY, subject=self.subj, ordering=0, planned_duration_minutes=60, title="")

    def _ids(self):
        return {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id}

    def test_rating_5_stars_stored_as_20(self):
        # Simulate bot mapping: 5 -> 20, but backend just stores whatever we send; we verify bot rating module
        # Here we PATCH via internal API with backend value 20
        resp = self.client.patch("/api/internal/v1/telegram/report/today/patch/", {**self._ids(), "self_rating": 20}, content_type="application/json", **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        report = DailyReport.objects.get(student=self.student, date=self.today)
        self.assertEqual(report.self_rating, 20)

    def test_rating_1_star_stored_as_4(self):
        resp = self.client.patch("/api/internal/v1/telegram/report/today/patch/", {**self._ids(), "self_rating": 4}, content_type="application/json", **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        report = DailyReport.objects.get(student=self.student, date=self.today)
        self.assertEqual(report.self_rating, 4)

    def test_subjects_endpoint_returns_valid(self):
        resp = self.client.get("/api/internal/v1/telegram/report/subjects/", {**self._ids()}, **self.svc)
        self.assertEqual(resp.status_code, 200, resp.data)
        names = [s["name"] for s in resp.data["subjects"]]
        self.assertIn("S1", names)

    def test_subjects_endpoint_returns_only_student_field_subjects(self):
        resp = self.client.get("/api/internal/v1/telegram/report/subjects/", {**self._ids()}, **self.svc)
        names = [s["name"] for s in resp.data["subjects"]]
        self.assertIn("S1", names)
        self.assertIn("S2", names)
        self.assertNotIn("Unrelated", names)

    def test_subjects_endpoint_excludes_other_grade_subject(self):
        resp = self.client.get("/api/internal/v1/telegram/report/subjects/", {**self._ids()}, **self.svc)
        ids = [s["id"] for s in resp.data["subjects"]]
        self.assertNotIn(self.unrelated_subj.pk, ids)

    def test_extra_activity_with_invalid_subject_rejected(self):
        resp = self.client.post("/api/internal/v1/telegram/report/extra-activity/", {**self._ids(), "kind": "STUDY", "subject": 99999, "actual_duration_minutes": 30, "duration_source": "MANUAL"}, **self.svc)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_extra_activity_with_valid_subject_ok(self):
        resp = self.client.post("/api/internal/v1/telegram/report/extra-activity/", {**self._ids(), "kind": "STUDY", "subject": self.subj.pk, "actual_duration_minutes": 30, "duration_source": "MANUAL"}, **self.svc)
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_extra_activity_with_unrelated_subject_rejected(self):
        resp = self.client.post("/api/internal/v1/telegram/report/extra-activity/", {**self._ids(), "kind": "STUDY", "subject": self.unrelated_subj.pk, "actual_duration_minutes": 30, "duration_source": "MANUAL"}, **self.svc)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("invalid_subject", str(resp.data).lower())
