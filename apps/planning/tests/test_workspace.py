from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Chapter, Field, Grade, Subject, Topic
from apps.planning.models import Plan, PlanDay, PlanItem

User = get_user_model()
START = date(2026, 10, 5)


class WorkspaceTests(APITestCase):
    def setUp(self):
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.other_counselor_user = User.objects.create_user("other", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.other_counselor = CounselorProfile.objects.create(user=self.other_counselor_user)
        self.student_user = User.objects.create_user("student")
        self.other_student_user = User.objects.create_user("other_student")
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        grade = Grade.objects.create(name="11")
        field = Field.objects.create(grade=grade, name="Math")
        self.field = field
        self.grade = grade
        self.subject = Subject.objects.create(field=field, name="Physics")
        self.chapter = Chapter.objects.create(subject=self.subject, name="Motion")
        self.topic = Topic.objects.create(chapter=self.chapter, name="Speed")
        future = timezone.localdate() + timedelta(days=5)
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=future, end_date=future + timedelta(days=6))
        self.day = PlanDay.objects.create(plan=self.plan, date=future)
        self.day2 = PlanDay.objects.create(plan=self.plan, date=future + timedelta(days=1))
        # also create plan for other counselor
        self.other_plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=future + timedelta(days=10), end_date=future + timedelta(days=16))

    def item(self, day=None, **kw):
        d = day or self.day
        data = {"plan_day": d, "kind": "STUDY", "subject": self.subject, "planned_duration_minutes": 60, "ordering": d.items.count()}
        data.update(kw)
        return PlanItem.objects.create(**data)

    def test_create_edit_own(self):
        self.client.force_authenticate(self.counselor_user)
        res = self.client.post("/api/v1/planning/items/", {"plan_day": self.day.pk, "kind": "STUDY", "subject": self.subject.pk, "planned_duration_minutes": 60, "ordering": 0})
        self.assertEqual(res.status_code, 201, res.data)
        iid = res.data["id"]
        res = self.client.patch(f"/api/v1/planning/items/{iid}/", {"planned_duration_minutes": 90})
        self.assertEqual(res.status_code, 200)

    def test_cannot_create_for_other_counselor_student(self):
        other_student = StudentProfile.objects.create(user=self.other_student_user, counselor=self.other_counselor)
        other_plan = Plan.objects.create(student=other_student, counselor=self.other_counselor, start_date=self.plan.start_date, end_date=self.plan.end_date)
        other_day = PlanDay.objects.create(plan=other_plan, date=self.plan.start_date)
        self.client.force_authenticate(self.counselor_user)
        res = self.client.post("/api/v1/planning/items/", {"plan_day": other_day.pk, "kind": "STUDY", "subject": self.subject.pk, "planned_duration_minutes": 60})
        self.assertEqual(res.status_code, 400)

    def test_move_between_days(self):
        a = self.item()
        b = self.item()
        self.client.force_authenticate(self.counselor_user)
        res = self.client.post(f"/api/v1/planning/items/{a.pk}/move/", {"target_day": self.day2.pk})
        self.assertEqual(res.status_code, 200, res.data)
        a.refresh_from_db()
        self.assertEqual(a.plan_day_id, self.day2.pk)

    def test_reject_invalid_target_day(self):
        a = self.item()
        self.client.force_authenticate(self.counselor_user)
        res = self.client.post(f"/api/v1/planning/items/{a.pk}/move/", {"target_day": 999999})
        self.assertEqual(res.status_code, 400)

    def test_duplicate_item(self):
        a = self.item(kind="TEST", test_count=15)
        self.client.force_authenticate(self.counselor_user)
        res = self.client.post(f"/api/v1/planning/items/{a.pk}/duplicate/", {})
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["subject"], self.subject.pk)
        self.assertEqual(res.data["test_count"], 15)

    def test_ordering_after_move(self):
        a = self.item()
        b = self.item()
        c = self.item(day=self.day2)
        self.client.force_authenticate(self.counselor_user)
        # reorder day
        res = self.client.post(f"/api/v1/planning/days/{self.day.pk}/reorder/", {"ordering": [b.pk, a.pk]})
        self.assertEqual(res.status_code, 200)
        b.refresh_from_db(); a.refresh_from_db()
        self.assertEqual(b.ordering, 0)
        self.assertEqual(a.ordering, 1)

    def test_invalid_academic_relationships(self):
        self.client.force_authenticate(self.counselor_user)
        other_subject = Subject.objects.create(field=self.field, name="Chem")
        res = self.client.post("/api/v1/planning/items/", {"plan_day": self.day.pk, "kind": "STUDY", "subject": other_subject.pk, "chapter": self.chapter.pk, "planned_duration_minutes": 60})
        self.assertEqual(res.status_code, 400)

    def test_academic_tree(self):
        self.client.force_authenticate(self.counselor_user)
        res = self.client.get(f"/api/v1/academics/tree/?student={self.student.pk}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("subjects", res.data)
