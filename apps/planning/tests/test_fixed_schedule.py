from datetime import date, timedelta, time

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Field, Grade, Subject
from apps.planning.models import Plan, PlanDay, PlanItem, StudentFixedCommitment, StudentSchoolSlot

User = get_user_model()
START = date(2026, 10, 5)


class FixedScheduleTests(APITestCase):
    def setUp(self):
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.other_counselor_user = User.objects.create_user("other_counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.other_counselor = CounselorProfile.objects.create(user=self.other_counselor_user)
        self.student_user = User.objects.create_user("student")
        self.other_student_user = User.objects.create_user("other_student")
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        self.other_student = StudentProfile.objects.create(user=self.other_student_user, counselor=self.other_counselor)
        grade = Grade.objects.create(name="12")
        field = Field.objects.create(grade=grade, name="Math")
        self.subject = Subject.objects.create(field=field, name="Physics")
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=START, end_date=START + timedelta(days=6))
        self.day = PlanDay.objects.create(plan=self.plan, date=START)

    def test_counselor_can_create_school_hours_batch(self):
        self.client.force_authenticate(self.counselor_user)
        r = self.client.post("/api/v1/planning/commitments/school-hours/", {"student": self.student.pk, "weekdays": [5, 6, 0], "start_time": "07:30", "end_time": "13:30"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data), 3)
        self.assertEqual(StudentFixedCommitment.objects.filter(student=self.student, kind="SCHOOL").count(), 3)

    def test_counselor_can_edit_school_hours_individual(self):
        self.client.force_authenticate(self.counselor_user)
        self.client.post("/api/v1/planning/commitments/school-hours/", {"student": self.student.pk, "weekdays": [5], "start_time": "07:30", "end_time": "13:30"}, format="json")
        c = StudentFixedCommitment.objects.get(student=self.student, weekday=5)
        r = self.client.patch(f"/api/v1/planning/commitments/{c.pk}/", {"start_time": "08:00", "end_time": "14:00"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        c.refresh_from_db()
        self.assertEqual(str(c.start_time)[:5], "08:00")

    def test_school_slots_optional(self):
        self.client.force_authenticate(self.counselor_user)
        # Only hours, no slots
        r = self.client.post("/api/v1/planning/commitments/school-hours/", {"student": self.student.pk, "weekdays": [5], "start_time": "07:30", "end_time": "13:30"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(StudentSchoolSlot.objects.filter(student=self.student).count(), 0)
        # Add slots
        r2 = self.client.post("/api/v1/planning/school-slots/bulk/", {"student": self.student.pk, "slots": [{"weekday": 5, "period": 1, "subject_name": "حسابان"}, {"weekday": 5, "period": 2, "subject_name": "فیزیک"}]}, format="json")
        self.assertEqual(r2.status_code, 200, r2.data)
        self.assertEqual(len([s for s in r2.data if s["weekday"] == 5]), 2)

    def test_batch_commitment_multi_day_atomic(self):
        self.client.force_authenticate(self.counselor_user)
        r = self.client.post("/api/v1/planning/commitments/batch/", {"student": self.student.pk, "kind": "CLASS", "title": "کلاس زبان", "weekdays": [5, 0], "start_time": "18:00", "end_time": "20:00"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        gid = r.data[0]["group_id"]
        self.assertIsNotNone(gid)
        self.assertEqual(r.data[0]["group_id"], r.data[1]["group_id"])
        # All share group
        self.assertEqual(StudentFixedCommitment.objects.filter(group_id=gid).count(), 2)
        # Group update
        r2 = self.client.post("/api/v1/planning/commitments/group-update/", {"group_id": gid, "title": "کلاس زبان جدید"}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(all(x["title"] == "کلاس زبان جدید" for x in r2.data))
        # Group delete
        r3 = self.client.post("/api/v1/planning/commitments/group-delete/", {"group_id": gid}, format="json")
        self.assertEqual(r3.status_code, 204)
        self.assertEqual(StudentFixedCommitment.objects.filter(group_id=gid).count(), 0)

    def test_toggle_active(self):
        self.client.force_authenticate(self.counselor_user)
        r = self.client.post("/api/v1/planning/commitments/", {"student": self.student.pk, "kind": "SPORTS", "title": "باشگاه", "weekday": 2, "start_time": "17:00", "end_time": "18:30"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        cid = r.data["id"]
        r2 = self.client.post(f"/api/v1/planning/commitments/{cid}/toggle-active/", {}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.data["active"])
        r3 = self.client.post(f"/api/v1/planning/commitments/{cid}/toggle-active/", {}, format="json")
        self.assertTrue(r3.data["active"])

    def test_counselor_cannot_access_other_counselor_student(self):
        self.client.force_authenticate(self.other_counselor_user)
        r = self.client.post("/api/v1/planning/commitments/", {"student": self.student.pk, "kind": "CLASS", "title": "X", "weekday": 0, "start_time": "10:00", "end_time": "11:00"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("assigned", str(r.data).lower() + str(r.data))
        r2 = self.client.post("/api/v1/planning/commitments/batch/", {"student": self.student.pk, "kind": "CLASS", "title": "X", "weekdays": [0], "start_time": "10:00", "end_time": "11:00"}, format="json")
        self.assertEqual(r2.status_code, 400)
        # Even reading other student commitments should be empty
        r3 = self.client.get(f"/api/v1/planning/commitments/?student={self.student.pk}")
        self.assertEqual(r3.data["count"], 0)

    def test_invalid_times_rejected(self):
        self.client.force_authenticate(self.counselor_user)
        r = self.client.post("/api/v1/planning/commitments/", {"student": self.student.pk, "kind": "CLASS", "title": "X", "weekday": 0, "start_time": "11:00", "end_time": "10:00"}, format="json")
        self.assertEqual(r.status_code, 400)
        r2 = self.client.post("/api/v1/planning/commitments/school-hours/", {"student": self.student.pk, "weekdays": [0], "start_time": "11:00", "end_time": "10:00"}, format="json")
        self.assertEqual(r2.status_code, 400)
        r3 = self.client.post("/api/v1/planning/commitments/batch/", {"student": self.student.pk, "kind": "CLASS", "title": "X", "weekdays": [0], "start_time": "11:00", "end_time": "11:00"}, format="json")
        self.assertEqual(r3.status_code, 400)

    def test_validity_window(self):
        self.client.force_authenticate(self.counselor_user)
        r = self.client.post("/api/v1/planning/commitments/batch/", {"student": self.student.pk, "kind": "CLASS", "title": "موقت", "weekdays": [1], "start_time": "10:00", "end_time": "11:00", "valid_from": "2026-10-01", "valid_until": "2026-11-01"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data[0]["valid_from"], "2026-10-01")
        r2 = self.client.post("/api/v1/planning/commitments/batch/", {"student": self.student.pk, "kind": "CLASS", "title": "X", "weekdays": [1], "start_time": "10:00", "end_time": "11:00", "valid_from": "2026-11-02", "valid_until": "2026-10-01"}, format="json")
        self.assertEqual(r2.status_code, 400)

    def test_batch_atomic_no_partial_on_invalid_weekday(self):
        self.client.force_authenticate(self.counselor_user)
        before = StudentFixedCommitment.objects.filter(student=self.student).count()
        r = self.client.post("/api/v1/planning/commitments/batch/", {"student": self.student.pk, "kind": "CLASS", "title": "X", "weekdays": [0, 0], "start_time": "10:00", "end_time": "11:00"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(StudentFixedCommitment.objects.filter(student=self.student).count(), before)

    def test_existing_commitments_preserved_after_migration_fields(self):
        # Simulate old data without group_id/valid fields still readable
        c = StudentFixedCommitment.objects.create(student=self.student, kind="SPORTS", title="Gym", weekday=0, start_time=time(17,0), end_time=time(18,0))
        self.client.force_authenticate(self.counselor_user)
        r = self.client.get(f"/api/v1/planning/commitments/?student={self.student.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(x["id"] == c.pk for x in r.data["results"]))

    def test_conflict_still_works_after_new_commitments(self):
        self.client.force_authenticate(self.counselor_user)
        # Create school commitment
        self.client.post("/api/v1/planning/commitments/school-hours/", {"student": self.student.pk, "weekdays": [START.weekday()], "start_time": "07:30", "end_time": "13:30"}, format="json")
        # Try to move/create overlapping plan item
        item = PlanItem.objects.create(plan_day=self.day, kind="STUDY", subject=self.subject, planned_duration_minutes=60, start_time=time(8,0), end_time=time(9,0))
        # Moving same item to same day should conflict with commitment
        r = self.client.post(f"/api/v1/planning/items/{item.pk}/move/", {"target_day": self.day.pk, "start_time": "08:30", "end_time": "09:30"}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "COMMITMENT_CONFLICT")

    def test_school_slot_permissions(self):
        self.client.force_authenticate(self.other_counselor_user)
        r = self.client.post("/api/v1/planning/school-slots/bulk/", {"student": self.student.pk, "slots": [{"weekday": 5, "period": 1, "subject_name": "ریاضی"}]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.client.force_authenticate(self.counselor_user)
        r2 = self.client.post("/api/v1/planning/school-slots/bulk/", {"student": self.student.pk, "slots": [{"weekday": 5, "period": 1, "subject_name": "ریاضی"}]}, format="json")
        self.assertEqual(r2.status_code, 200)
