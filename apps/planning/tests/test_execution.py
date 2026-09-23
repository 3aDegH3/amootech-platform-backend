from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Grade, Field, Subject
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution

User = get_user_model()


class PlanExecutionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=counselor_user)
        self.user = User.objects.create_user("student", role=User.Role.STUDENT)
        self.student = StudentProfile.objects.create(user=self.user, counselor=self.counselor)
        self.other_user = User.objects.create_user("other", role=User.Role.STUDENT)
        StudentProfile.objects.create(user=self.other_user, counselor=self.counselor)
        grade = Grade.objects.create(name="12")
        field = Field.objects.create(grade=grade, name="Math")
        subject = Subject.objects.create(field=field, name="Calculus")
        today = timezone.localdate()
        self.plan = Plan.objects.create(
            student=self.student, counselor=self.counselor,
            start_date=today, end_date=today, status=Plan.Status.PUBLISHED,
            published_at=timezone.now(),
        )
        day = PlanDay.objects.create(plan=self.plan, date=today)
        self.item = PlanItem.objects.create(plan_day=day, kind="STUDY", subject=subject, planned_duration_minutes=90)
        self.second = PlanItem.objects.create(plan_day=day, kind="TEST", subject=subject, test_count=20, planned_duration_minutes=60)
        self.event = PlanItem.objects.create(plan_day=day, kind="EVENT", title="Gym")
        self.client.force_authenticate(self.user)

    def action(self, item, name, data=None):
        return self.client.post(f"/api/v1/planning/items/{item.pk}/{name}/", data or {}, format="json")

    def test_timer_accumulates_only_active_time_and_survives_refetch(self):
        base = datetime(2026, 9, 22, 18, 10, tzinfo=dt_timezone.utc)
        with patch("apps.planning.execution.timezone.now", return_value=base):
            started = self.action(self.item, "start")
        self.assertEqual(started.status_code, 200, started.data)
        self.assertEqual(started.data["status"], "IN_PROGRESS")
        self.assertEqual(started.data["completion_method"], "TIMER")
        with patch("apps.planning.views.timezone.now", return_value=base + timedelta(minutes=15)):
            running = self.client.get(f"/api/v1/planning/plans/{self.plan.pk}/executions/")
        self.assertEqual(running.data[0]["elapsed_seconds"], 900)
        with patch("apps.planning.execution.timezone.now", return_value=base + timedelta(minutes=30)):
            paused = self.action(self.item, "pause")
        self.assertEqual(paused.data["elapsed_seconds"], 1800)
        self.assertEqual(paused.data["status"], "PAUSED")
        with patch("apps.planning.views.timezone.now", return_value=base + timedelta(minutes=50)):
            fetched = self.client.get(f"/api/v1/planning/plans/{self.plan.pk}/executions/")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.data[0]["elapsed_seconds"], 1800)
        with patch("apps.planning.execution.timezone.now", return_value=base + timedelta(minutes=50)):
            resumed = self.action(self.item, "resume")
        self.assertEqual(resumed.data["status"], "IN_PROGRESS")
        with patch("apps.planning.execution.timezone.now", return_value=base + timedelta(minutes=95)):
            finished = self.action(self.item, "finish", {"complete": True})
        self.assertEqual(finished.status_code, 200, finished.data)
        self.assertEqual(finished.data["status"], "COMPLETED")
        self.assertEqual(finished.data["elapsed_seconds"], 4500)
        self.assertIsNotNone(finished.data["completed_at"])
        self.assertIsNone(finished.data["current_session_started_at"])
        self.assertEqual(PlanItemExecution.objects.get(plan_item=self.item).accumulated_seconds, 4500)
        self.assertEqual(self.action(self.item, "resume").status_code, 400)
        self.assertEqual(self.action(self.item, "start").status_code, 400)

    def test_partial_and_quick_complete(self):
        self.assertEqual(self.action(self.item, "start").status_code, 200)
        self.assertEqual(self.action(self.item, "finish", {"complete": False}).data["status"], "PARTIAL")
        self.assertEqual(self.action(self.second, "quick-complete").data["status"], "COMPLETED")
        quick = PlanItemExecution.objects.get(plan_item=self.second)
        self.assertIsNone(quick.started_at)
        self.assertEqual(quick.accumulated_seconds, 0)
        self.assertEqual(self.action(self.second, "quick-complete").status_code, 200)
        self.assertEqual(PlanItemExecution.objects.filter(plan_item=self.second).count(), 1)
        self.assertEqual(self.action(self.event, "quick-complete").status_code, 200)
        self.assertEqual(self.action(self.event, "start").status_code, 400)

    def test_invalid_transitions_and_second_active_timer(self):
        self.assertEqual(self.action(self.item, "pause").status_code, 400)
        self.assertEqual(self.action(self.item, "resume").status_code, 400)
        self.assertEqual(self.action(self.item, "finish", {"complete": True}).status_code, 400)
        self.assertEqual(self.action(self.item, "start").status_code, 200)
        self.assertEqual(self.action(self.item, "start").status_code, 200)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlanItemExecution.objects.create(
                student=self.student, plan_item=self.second,
                status=PlanItemExecution.Status.IN_PROGRESS,
                started_at=timezone.now(), current_session_started_at=timezone.now(),
            )
        conflict = self.action(self.second, "start")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(int(conflict.data["active_item"]), self.item.pk)
        self.assertEqual(self.action(self.item, "pause").status_code, 200)
        self.assertEqual(self.action(self.item, "pause").status_code, 400)
        self.assertEqual(self.action(self.second, "start").status_code, 200)
        self.assertEqual(self.action(self.item, "resume").status_code, 409)
        self.assertEqual(self.action(self.second, "finish", {}).status_code, 400)

    def test_ownership_unpublished_and_role_permissions(self):
        url = f"/api/v1/planning/items/{self.item.pk}/start/"
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/planning/plans/{self.plan.pk}/executions/").status_code, 404)
        self.client.force_authenticate(self.counselor.user)
        self.assertEqual(self.client.post(url).status_code, 403)
        admin = User.objects.create_user("admin", role=User.Role.ADMIN)
        self.client.force_authenticate(admin)
        self.assertEqual(self.client.get(f"/api/v1/planning/plans/{self.plan.pk}/executions/").status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.post(url).status_code, 401)
        self.plan.status = Plan.Status.DRAFT
        self.plan.published_at = None
        self.plan.save()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/planning/plans/{self.plan.pk}/executions/").status_code, 404)
