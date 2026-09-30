from datetime import timedelta
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Subject, Field, Grade
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution, StudentFixedCommitment

User = get_user_model()

class CopyDayTests(APITestCase):
    def setUp(self):
        self.counselor = User.objects.create_user("c1", role=User.Role.COUNSELOR)
        self.counselor2 = User.objects.create_user("c2", role=User.Role.COUNSELOR)
        self.cp1 = CounselorProfile.objects.create(user=self.counselor)
        self.cp2 = CounselorProfile.objects.create(user=self.counselor2)
        self.su = User.objects.create_user("stu")
        self.sp = StudentProfile.objects.create(user=self.su, counselor=self.cp1)
        grade = Grade.objects.create(name="G1")
        field = Field.objects.create(grade=grade, name="F1")
        self.subj = Subject.objects.create(field=field, name="Math")
        future = timezone.localdate() + timedelta(days=5)
        self.plan = Plan.objects.create(student=self.sp, counselor=self.cp1, start_date=future, end_date=future+timedelta(days=6))
        self.day1 = PlanDay.objects.create(plan=self.plan, date=future)
        self.day2 = PlanDay.objects.create(plan=self.plan, date=future+timedelta(days=1))
        self.item1 = PlanItem.objects.create(plan_day=self.day1, kind="STUDY", subject=self.subj, planned_duration_minutes=60, note="n1", ordering=0, start_time="09:00", end_time="10:00")
        self.item2 = PlanItem.objects.create(plan_day=self.day1, kind="TEST", subject=self.subj, planned_duration_minutes=30, test_count=5, ordering=1, start_time="11:00", end_time="11:30")

    def test_copy_to_empty(self):
        self.client.force_authenticate(self.counselor)
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day2.pk, "mode": "append"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.day2.items.count(), 2)
        self.assertEqual(self.day1.items.count(), 2)

    def test_append_to_nonempty(self):
        PlanItem.objects.create(plan_day=self.day2, kind="STUDY", subject=self.subj, planned_duration_minutes=60, ordering=0, start_time="14:00", end_time="15:00")
        self.client.force_authenticate(self.counselor)
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day2.pk, "mode": "append"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.day2.items.count(), 3)

    def test_replace(self):
        PlanItem.objects.create(plan_day=self.day2, kind="STUDY", subject=self.subj, planned_duration_minutes=60, ordering=0)
        self.client.force_authenticate(self.counselor)
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day2.pk, "mode": "replace"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.day2.items.count(), 2)

    def test_replace_blocked_if_protected(self):
        it = PlanItem.objects.create(plan_day=self.day2, kind="STUDY", subject=self.subj, planned_duration_minutes=60, ordering=0)
        PlanItemExecution.objects.create(student=self.sp, plan_item=it, status="COMPLETED")
        self.client.force_authenticate(self.counselor)
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day2.pk, "mode": "replace"})
        self.assertEqual(r.status_code, 409, r.data)

    def test_time_conflict(self):
        PlanItem.objects.create(plan_day=self.day2, kind="STUDY", subject=self.subj, planned_duration_minutes=60, ordering=0, start_time="09:30", end_time="10:30")
        self.client.force_authenticate(self.counselor)
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day2.pk, "mode": "append"})
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["code"], "COPY_CONFLICT")

    def test_no_execution_copied(self):
        PlanItemExecution.objects.create(student=self.sp, plan_item=self.item1, status="COMPLETED")
        self.client.force_authenticate(self.counselor)
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day2.pk, "mode": "append"})
        self.assertEqual(r.status_code, 200)
        for it in self.day2.items.all():
            self.assertFalse(hasattr(it, "execution"))

    def test_other_counselor_forbidden(self):
        self.client.force_authenticate(self.counselor2)
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day2.pk})
        self.assertIn(r.status_code, [403,404])

    def test_rollback_on_partial_failure(self):
        StudentFixedCommitment.objects.create(student=self.sp, kind="SCHOOL", title="School", weekday=self.day2.date.weekday(), start_time="09:00", end_time="09:30")
        self.client.force_authenticate(self.counselor)
        before = self.day2.items.count()
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day2.pk})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.day2.items.count(), before)

    def test_self_copy_rejected(self):
        self.client.force_authenticate(self.counselor)
        r = self.client.post(f"/api/v1/planning/days/{self.day1.pk}/copy-day/", {"target_day": self.day1.pk})
        self.assertEqual(r.status_code, 400)
