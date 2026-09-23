from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Chapter, Field, Grade, Subject, Topic
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.daily_reports.reporting import reporting_data
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution, StudentFixedCommitment

User = get_user_model()
START = date(2026, 10, 5)


class PlanningTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", role=User.Role.ADMIN)
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.other_counselor_user = User.objects.create_user("other_counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.other_counselor = CounselorProfile.objects.create(user=self.other_counselor_user)
        self.student_user = User.objects.create_user("student")
        self.other_student_user = User.objects.create_user("other_student")
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        self.other_student = StudentProfile.objects.create(user=self.other_student_user, counselor=self.other_counselor)
        grade = Grade.objects.create(name="11")
        field = Field.objects.create(grade=grade, name="Math")
        self.subject = Subject.objects.create(field=field, name="Physics")
        self.other_subject = Subject.objects.create(field=field, name="Chemistry")
        self.chapter = Chapter.objects.create(subject=self.subject, name="Motion")
        self.topic = Topic.objects.create(chapter=self.chapter, name="Speed")
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=START, end_date=START + timedelta(days=6))
        self.day = PlanDay.objects.create(plan=self.plan, date=START)

    def item(self, **changes):
        data = {"plan_day": self.day, "kind": PlanItem.Kind.STUDY, "subject": self.subject, "planned_duration_minutes": 90}
        data.update(changes)
        return PlanItem.objects.create(**data)

    def test_model_validations_and_types(self):
        self.assertEqual(self.plan.status, Plan.Status.DRAFT)
        with self.assertRaises(ValidationError):
            Plan.objects.create(student=self.student, counselor=self.counselor, start_date=START + timedelta(days=2), end_date=START)
        with self.assertRaises(ValidationError):
            Plan.objects.create(student=self.other_student, counselor=self.counselor, start_date=START, end_date=START)
        with self.assertRaises(ValidationError):
            PlanDay.objects.create(plan=self.plan, date=START)
        with self.assertRaises(ValidationError):
            PlanDay.objects.create(plan=self.plan, date=START + timedelta(days=7))
        for kind in (PlanItem.Kind.STUDY, PlanItem.Kind.REVIEW):
            self.assertEqual(self.item(kind=kind).kind, kind)
        self.assertEqual(self.item(kind=PlanItem.Kind.TEST, test_count=20).test_count, 20)
        self.assertEqual(self.item(kind=PlanItem.Kind.EXAM, title="Mock exam", subject=None).kind, PlanItem.Kind.EXAM)
        self.assertEqual(self.item(kind=PlanItem.Kind.EVENT, title="Sports", subject=None).kind, PlanItem.Kind.EVENT)
        self.assertIsNone(self.item(kind=PlanItem.Kind.EVENT, title="Commute", subject=None, planned_duration_minutes=None).planned_duration_minutes)
        flexible = self.item()
        self.assertIsNone(flexible.start_time)
        scheduled = self.item(kind=PlanItem.Kind.EVENT, title="Gym", subject=None, start_time=time(15, 30), end_time=time(17, 30))
        self.assertEqual(scheduled.end_time, time(17, 30))
        with self.assertRaises(ValidationError):
            self.item(kind=PlanItem.Kind.TEST, test_count=0)
        with self.assertRaises(ValidationError):
            self.item(planned_duration_minutes=None)
        with self.assertRaises(ValidationError):
            self.item(chapter=self.chapter, subject=self.other_subject)
        with self.assertRaises(ValidationError):
            self.item(topic=self.topic, subject=self.other_subject)
        with self.assertRaises(ValidationError):
            self.item(kind=PlanItem.Kind.EVENT, title="", subject=None)
        with self.assertRaises(ValidationError):
            self.item(start_time=time(15, 30))

    def test_commitment_model_validation(self):
        with self.assertRaises(ValidationError):
            StudentFixedCommitment.objects.create(student=self.student, kind="SPORTS", title="Gym", weekday=7, start_time=time(17), end_time=time(18))
        with self.assertRaises(ValidationError):
            StudentFixedCommitment.objects.create(student=self.student, kind="SPORTS", title="Gym", weekday=0, start_time=time(18), end_time=time(17))

    def test_plan_permissions_and_publish(self):
        plans = "/api/v1/planning/plans/"
        self.assertEqual(self.client.get(plans).status_code, 401)
        self.client.force_authenticate(self.other_counselor_user)
        self.assertEqual(self.client.get(f"{plans}{self.plan.pk}/").status_code, 404)
        response = self.client.post(plans, {"student": self.student.pk, "start_date": str(START), "end_date": str(START)})
        self.assertEqual(response.status_code, 400)
        self.client.force_authenticate(self.counselor_user)
        response = self.client.post(plans, {"student": self.student.pk, "start_date": str(START), "end_date": str(START + timedelta(days=2))})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counselor"], self.counselor.pk)
        self.assertEqual(self.client.patch(f"{plans}{self.plan.pk}/", {"status": "PUBLISHED"}).status_code, 400)
        self.assertEqual(self.client.post(f"{plans}{self.plan.pk}/publish/").status_code, 400)
        item = self.item()
        response = self.client.post(f"{plans}{self.plan.pk}/publish/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.data["published_at"])
        self.client.force_authenticate(self.student_user)
        self.assertEqual(self.client.get(plans).data["count"], 1)
        self.assertEqual(self.client.get(f"{plans}{self.plan.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"{plans}{response.data['id'] + 1}/").status_code, 404)
        self.assertEqual(self.client.patch(f"{plans}{self.plan.pk}/", {"title": "Changed"}).status_code, 403)
        self.assertEqual(self.client.post(plans, {"student": self.student.pk}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/planning/days/{self.day.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/planning/items/{item.pk}/").status_code, 200)
        self.assertEqual(self.client.patch(f"/api/v1/planning/days/{self.day.pk}/", {"date": str(START + timedelta(days=1))}).status_code, 403)
        self.assertEqual(self.client.patch(f"/api/v1/planning/items/{item.pk}/", {"ordering": 5}).status_code, 403)
        self.client.force_authenticate(self.other_student_user)
        self.assertEqual(self.client.get(f"{plans}{self.plan.pk}/").status_code, 404)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(f"{plans}{self.plan.pk}/").status_code, 200)
        self.assertEqual(self.client.patch(f"{plans}{self.plan.pk}/", {"title": "Support edit"}).status_code, 200)
        self.assertEqual(self.client.patch(f"{plans}{self.plan.pk}/", {"start_date": str(START + timedelta(days=1))}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/v1/planning/items/{item.pk}/", {"ordering": 5}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/planning/items/{item.pk}/").status_code, 204)

    def test_day_item_api_and_copy_independence(self):
        self.client.force_authenticate(self.counselor_user)
        days = "/api/v1/planning/days/"
        items = "/api/v1/planning/items/"
        response = self.client.post(days, {"plan": self.plan.pk, "date": str(START + timedelta(days=1))})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.client.post(days, {"plan": self.plan.pk, "date": str(START)}).status_code, 400)
        self.assertEqual(self.client.post(days, {"plan": self.plan.pk, "date": str(START + timedelta(days=7))}).status_code, 400)
        cases = (
            {"kind": "STUDY", "subject": self.subject.pk},
            {"kind": "TEST", "subject": self.subject.pk, "test_count": 20},
            {"kind": "REVIEW", "subject": self.subject.pk},
            {"kind": "EXAM", "title": "Mock exam"},
            {"kind": "EVENT", "title": "Sports", "start_time": "15:30", "end_time": "17:30"},
        )
        for ordering, details in enumerate(cases):
            response = self.client.post(items, {"plan_day": self.day.pk, "planned_duration_minutes": 60, "ordering": ordering, **details})
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.client.post(items, {"plan_day": self.day.pk, "kind": "TEST", "subject": self.subject.pk, "planned_duration_minutes": 60}).status_code, 400)
        self.assertEqual(self.client.post(items, {"plan_day": self.day.pk, "kind": "STUDY", "subject": self.other_subject.pk, "chapter": self.chapter.pk, "planned_duration_minutes": 60}).status_code, 400)
        response = self.client.post(items, {"plan_day": self.day.pk, "kind": "EVENT", "title": "Commute"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["planned_duration_minutes"])
        detail = self.client.get(f"/api/v1/planning/plans/{self.plan.pk}/")
        self.assertEqual(detail.data["days"][0]["items"][0]["subject_name"], "Physics")
        copied_day_response = self.client.post(f"{days}{self.day.pk}/duplicate/", {"date": str(START + timedelta(days=2))})
        self.assertEqual(copied_day_response.status_code, 201, copied_day_response.data)
        self.assertEqual(len(copied_day_response.data["items"]), 6)
        copied_day = PlanDay.objects.get(pk=copied_day_response.data["id"])
        self.assertNotEqual(self.day.items.first().pk, copied_day.items.first().pk)
        copied_plan_response = self.client.post(f"/api/v1/planning/plans/{self.plan.pk}/duplicate/")
        self.assertEqual(copied_plan_response.status_code, 201, copied_plan_response.data)
        copied_plan = Plan.objects.get(pk=copied_plan_response.data["id"])
        self.assertEqual(copied_plan.status, Plan.Status.DRAFT)
        self.assertEqual(copied_plan.days.count(), 3)
        copied_item = copied_plan.days.get(date=START).items.first()
        copied_item.title = "Changed copy"
        copied_item.save()
        self.assertNotEqual(self.day.items.first().title, copied_item.title)

    def test_nested_resources_are_scoped(self):
        item = self.item()
        self.client.force_authenticate(self.other_counselor_user)
        self.assertEqual(self.client.get(f"/api/v1/planning/days/{self.day.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/planning/items/{item.pk}/").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/planning/items/", {"plan_day": self.day.pk, "kind": "STUDY", "subject": self.subject.pk, "planned_duration_minutes": 60}).status_code, 400)
        self.client.force_authenticate(self.student_user)
        self.assertEqual(self.client.get(f"/api/v1/planning/days/{self.day.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/planning/items/{item.pk}/").status_code, 404)

    def test_fixed_commitment_permissions(self):
        path = "/api/v1/planning/commitments/"
        self.client.force_authenticate(self.student_user)
        response = self.client.post(path, {"kind": "SPORTS", "title": "Gym", "weekday": 0, "start_time": "15:30", "end_time": "17:30"})
        self.assertEqual(response.status_code, 201, response.data)
        commitment_id = response.data["id"]
        self.assertEqual(response.data["student"], self.student.pk)
        self.assertEqual(self.client.post(path, {"student": self.other_student.pk, "kind": "SPORTS", "title": "Other", "weekday": 0, "start_time": "15:30", "end_time": "17:30"}).status_code, 400)
        self.client.force_authenticate(self.counselor_user)
        self.assertEqual(self.client.get(path).data["count"], 1)
        self.assertEqual(self.client.patch(f"{path}{commitment_id}/", {"title": "Changed"}).status_code, 403)
        self.client.force_authenticate(self.other_counselor_user)
        self.assertEqual(self.client.get(path).data["count"], 0)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.patch(f"{path}{commitment_id}/", {"active": False}).status_code, 200)

    def test_plan_detail_query_count(self):
        for offset in range(4):
            day = PlanDay.objects.create(plan=self.plan, date=START + timedelta(days=offset + 1))
            PlanItem.objects.create(plan_day=day, kind="STUDY", subject=self.subject, chapter=self.chapter, topic=self.topic, planned_duration_minutes=60)
        self.client.force_authenticate(self.counselor_user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"/api/v1/planning/plans/{self.plan.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["days"]), 5)
        self.assertLessEqual(len(queries), 6)

    def test_past_today_future_edit_policy_and_execution_safety(self):
        today = START + timedelta(days=2)
        policy_plan = Plan.objects.create(
            student=self.student, counselor=self.counselor, start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=2), status=Plan.Status.PUBLISHED, published_at=timezone.now(),
        )
        past_day = PlanDay.objects.create(plan=policy_plan, date=today - timedelta(days=1))
        today_day = PlanDay.objects.create(plan=policy_plan, date=today)
        future_day = PlanDay.objects.create(plan=policy_plan, date=today + timedelta(days=1))
        past_item = PlanItem.objects.create(plan_day=past_day, kind="STUDY", subject=self.subject, planned_duration_minutes=60)
        today_item = PlanItem.objects.create(plan_day=today_day, kind="STUDY", subject=self.subject, planned_duration_minutes=90)
        future_item = PlanItem.objects.create(plan_day=future_day, kind="STUDY", subject=self.subject, planned_duration_minutes=60)
        self.client.force_authenticate(self.counselor_user)
        items = "/api/v1/planning/items/"
        with patch("apps.planning.serializers.timezone.localdate", return_value=today), patch("apps.planning.views.timezone.localdate", return_value=today):
            self.assertEqual(self.client.patch(f"{items}{past_item.pk}/", {"note": "late edit"}).status_code, 400)
            self.assertEqual(self.client.delete(f"{items}{past_item.pk}/").status_code, 409)
            self.assertEqual(self.client.post(items, {"plan_day": past_day.pk, "kind": "STUDY", "subject": self.subject.pk, "planned_duration_minutes": 30}).status_code, 400)
            edited = self.client.patch(f"{items}{today_item.pk}/", {"planned_duration_minutes": 100, "start_time": "10:00", "end_time": "11:40"})
            self.assertEqual(edited.status_code, 200, edited.data)
            added = self.client.post(items, {"plan_day": today_day.pk, "kind": "REVIEW", "subject": self.subject.pk, "planned_duration_minutes": 30})
            self.assertEqual(added.status_code, 201, added.data)
            self.assertEqual(self.client.delete(f"{items}{added.data['id']}/").status_code, 204)
            self.assertEqual(self.client.patch(f"{items}{future_item.pk}/", {"planned_duration_minutes": 75}).status_code, 200)
            PlanItemExecution.objects.create(
                student=self.student, plan_item=today_item, status=PlanItemExecution.Status.PARTIAL,
                started_at=timezone.now(), accumulated_seconds=60 * 60,
            )
            report = DailyReport.objects.create(student=self.student, date=today)
            DailyReportItem.objects.create(report=report, plan_item=today_item, actual_duration_minutes=60, duration_source="MANUAL")
            rejected = self.client.patch(f"{items}{today_item.pk}/", {"subject": self.other_subject.pk, "planned_duration_minutes": 30})
            self.assertEqual(rejected.status_code, 400)
            self.assertIn("دیگر قابل ویرایش", str(rejected.data))
            self.assertEqual(self.client.patch(f"/api/v1/planning/days/{today_day.pk}/", {"date": str(today + timedelta(days=2))}).status_code, 400)
            self.assertEqual(self.client.delete(f"{items}{today_item.pk}/").status_code, 409)
            today_item.refresh_from_db()
            self.assertEqual(today_item.planned_duration_minutes, 100)
            report_data = reporting_data(self.student, today, today, "all")
            self.assertEqual(report_data["summary"]["planned_minutes"], 100)
            self.assertEqual(report_data["summary"]["actual_minutes"], 60)
            detail = self.client.get(f"/api/v1/planning/plans/{policy_plan.pk}/")
            locked = next(row for row in detail.data["days"][1]["items"] if row["id"] == today_item.pk)
            self.assertFalse(locked["counselor_editable"])
        self.client.force_authenticate(self.other_counselor_user)
        self.assertEqual(self.client.patch(f"{items}{future_item.pk}/", {"note": "forbidden"}).status_code, 404)
