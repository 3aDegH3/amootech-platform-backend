import io
import os
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.daily_reports.reporting import reporting_data
from apps.planning.models import Plan, PlanItem, PlanItemExecution


class DemoSeedTests(TestCase):
    def seed(self, **kwargs):
        with patch.dict(os.environ, {"ALLOW_DEMO_SEED": "true"}):
            options = {"counselors": 2, "students_per_counselor": 2, "history_days": 8, **kwargs}
            call_command("seed_demo", stdout=io.StringIO(), **options)

    def test_guard_and_invalid_options(self):
        with patch.dict(os.environ, {"ALLOW_DEMO_SEED": ""}):
            with self.assertRaises(CommandError):
                call_command("seed_demo", stdout=io.StringIO())
        with self.assertRaises(CommandError):
            self.seed(history_days=0)

    def test_people_plans_history_aggregation_and_idempotency(self):
        self.seed()
        self.assertTrue(User.objects.get(username="demo_admin").check_password("Demo12345!"))
        self.assertEqual(CounselorProfile.objects.filter(user__username__startswith="demo_counselor").count(), 2)
        students = list(StudentProfile.objects.filter(user__username__startswith="demo_student").order_by("user__username"))
        self.assertEqual(len(students), 4)
        self.assertEqual([student.counselor_id for student in students[:2]], [students[0].counselor_id] * 2)
        self.assertNotEqual(students[0].counselor_id, students[2].counselor_id)
        self.assertTrue(all(Plan.objects.filter(student=student, status="PUBLISHED",
            start_date__lte=timezone.localdate(), end_date__gte=timezone.localdate()).exists() for student in students))
        self.assertTrue(DailyReport.objects.filter(student=students[0]).exists())
        self.assertTrue(PlanItemExecution.objects.filter(student=students[0]).exists())
        self.assertTrue(DailyReportItem.objects.filter(report__student=students[0], plan_item__isnull=True).exists())
        sample = DailyReportItem.objects.filter(report__student=students[0], plan_item__isnull=True).first()
        sample.full_clean()
        start = timezone.localdate() - timedelta(days=7)
        result = reporting_data(students[0], start, timezone.localdate(), "all")
        self.assertGreater(result["summary"]["planned_minutes"], 0)
        self.assertEqual(len(result["trend"]), 8)
        self.assertTrue(result["subjects"])
        self.assertTrue(result["topic_repetition"])
        counts = (User.objects.filter(username__startswith="demo_").count(), Plan.objects.count(),
                  PlanItem.objects.count(), DailyReport.objects.count(), DailyReportItem.objects.count())
        self.seed()
        self.assertEqual(counts, (User.objects.filter(username__startswith="demo_").count(),
            Plan.objects.count(), PlanItem.objects.count(), DailyReport.objects.count(), DailyReportItem.objects.count()))
        repeated = reporting_data(StudentProfile.objects.get(user__username="demo_student"),
                                  start, timezone.localdate(), "all")
        self.assertEqual(result["summary"], repeated["summary"])
        self.assertEqual(result["trend"], repeated["trend"])

        client = APIClient()
        client.force_authenticate(User.objects.get(username="demo_counselor"))
        self.assertEqual(client.get(f"/api/v1/counselor/students/{students[2].pk}/progress/").status_code, 404)
        self.assertEqual(client.get(f"/api/v1/counselor/students/{students[0].pk}/progress/").status_code, 200)

    def test_reset_preserves_non_demo_user(self):
        real_counselor_user = User.objects.create_user("real_counselor", role=User.Role.COUNSELOR)
        real_counselor = CounselorProfile.objects.create(user=real_counselor_user)
        real = User.objects.create_user("real_student", role=User.Role.STUDENT)
        real_profile = StudentProfile.objects.create(user=real, counselor=real_counselor)
        real_plan = Plan.objects.create(student=real_profile, counselor=real_counselor,
                                        start_date=timezone.localdate(), end_date=timezone.localdate())
        real_report = DailyReport.objects.create(student=real_profile, date=timezone.localdate())
        self.seed()
        self.seed(reset=True)
        self.assertTrue(User.objects.filter(pk=real.pk, username="real_student").exists())
        self.assertTrue(StudentProfile.objects.filter(user=real).exists())
        self.assertTrue(Plan.objects.filter(pk=real_plan.pk).exists())
        self.assertTrue(DailyReport.objects.filter(pk=real_report.pk).exists())
        self.assertEqual(User.objects.filter(username__startswith="demo_student").count(), 4)
