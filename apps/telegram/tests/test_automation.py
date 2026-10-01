from unittest.mock import patch

from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.daily_reports.services import close_day_review, open_report
from apps.planning.execution import transition
from apps.planning.models import Plan, PlanItemExecution
from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection
from . import test_report_api


@override_settings(TELEGRAM_BOT_SERVICE_TOKEN="svc-secret")
class AutomationTests(APITestCase):
    setUp = test_report_api.TelegramReportTests.setUp
    _ids = test_report_api.TelegramReportTests._ids

    def listing(self, evening=False):
        kind = "end-of-day" if evening else "morning"
        response = self.client.get(f"/api/internal/v1/telegram/automation/{kind}-recipients/", {"date": self.today.isoformat()}, **self.svc)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["recipients"]

    def test_morning_returns_only_telegram_identity(self):
        self.assertEqual(self.listing(), [self._ids()])

    def test_disabled_suspended_banned_connections_are_excluded(self):
        connection = TelegramStudentConnection.objects.get(student=self.student)
        for name, value in (("is_enabled", False), ("is_suspended", True), ("is_banned", True), ("is_active", False)):
            with self.subTest(name=name):
                old = getattr(connection, name)
                setattr(connection, name, value)
                connection.save()
                self.assertEqual(self.listing(), [])
                setattr(connection, name, old)
                connection.save()

    def test_inactive_group_and_student_are_excluded(self):
        group = TelegramGroupConnection.objects.get(student=self.student)
        group.is_active = False
        group.save()
        self.assertEqual(self.listing(), [])
        group.is_active = True
        group.save()
        self.student_user.is_active = False
        self.student_user.save()
        self.assertEqual(self.listing(), [])

    def test_draft_and_missing_day_are_excluded(self):
        self.plan.status = Plan.Status.DRAFT
        self.plan.published_at = None
        self.plan.save()
        self.assertEqual(self.listing(), [])
        self.plan.status = Plan.Status.PUBLISHED
        self.plan.published_at = timezone.now()
        self.plan.save()
        self.day.delete()
        self.assertEqual(self.listing(), [])

    def test_evening_reuses_close_review_without_creating_reports(self):
        self.assertEqual(self.listing(True), [{**self._ids(), "unresolved_count": 2}])
        self.assertEqual(len(close_day_review(self.student, self.today)["unresolved"]), 2)
        self.assertFalse(DailyReport.objects.exists())
        for item in (self.item1, self.item2):
            transition(self.student_user, item.pk, "mark_not_done")
        self.assertEqual(self.listing(True), [])

    def test_finalized_day_excluded_even_if_new_unresolved_item_exists(self):
        DailyReport.objects.create(student=self.student, date=self.today, closed_at=timezone.now())
        self.assertEqual(self.listing(True), [])

    def test_automation_service_auth_and_valid_date_required(self):
        for kind in ("morning", "end-of-day"):
            url = f"/api/internal/v1/telegram/automation/{kind}-recipients/"
            self.assertEqual(self.client.get(url, {"date": str(self.today)}).status_code, 403)
            self.assertEqual(self.client.get(url, {"date": "bad"}, **self.svc).status_code, 400)

    def test_web_defaults_and_telegram_execution_source_then_web_update(self):
        report = open_report(self.student_user, self.today)
        self.assertEqual(report.source, "WEB")
        self.assertEqual(report.items.first().source, "WEB")
        exe = transition(self.student_user, self.item1.pk, "quick_complete")
        self.assertEqual(exe.source, "WEB")
        response = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {
            **self._ids(), "plan_item_id": self.item1.pk, "status": "PARTIAL", "actual_duration_minutes": 20,
        }, format="json", **self.svc)
        self.assertEqual(response.status_code, 200, response.data)
        exe.refresh_from_db()
        self.assertEqual(exe.source, "TELEGRAM")
        row = DailyReportItem.objects.get(plan_item=self.item1)
        self.assertEqual(row.source, "TELEGRAM")
        transition(self.student_user, self.item1.pk, "quick_complete")
        exe.refresh_from_db()
        self.assertEqual(exe.source, "WEB")
        self.client.force_authenticate(self.student_user)
        response = self.client.put(f"/api/v1/daily-reports/{report.pk}/planned/{self.item1.pk}/", {"note": "website correction"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        row.refresh_from_db()
        self.assertEqual(row.source, "WEB")

    def test_telegram_patch_extra_finalize_and_web_patch_sources(self):
        response = self.client.patch("/api/internal/v1/telegram/report/today/patch/", {**self._ids(), "mobile_minutes": 30, "self_rating": 15}, format="json", **self.svc)
        self.assertEqual(response.status_code, 200, response.data)
        report = DailyReport.objects.get(student=self.student, date=self.today)
        self.assertEqual(report.source, "TELEGRAM")
        response = self.client.post("/api/internal/v1/telegram/report/extra-activity/", {
            **self._ids(), "kind": "STUDY", "subject": self.subj.pk, "actual_duration_minutes": 10, "duration_source": "MANUAL",
        }, format="json", **self.svc)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(report.items.get(plan_item__isnull=True).source, "TELEGRAM")
        for item in (self.item1, self.item2):
            transition(self.student_user, item.pk, "mark_not_done")
        response = self.client.post("/api/internal/v1/telegram/report/today/finalize/", self._ids(), format="json", **self.svc)
        self.assertEqual(response.status_code, 200, response.data)
        report.refresh_from_db()
        self.assertEqual(report.source, "TELEGRAM")
        self.client.force_authenticate(self.student_user)
        response = self.client.patch(f"/api/v1/daily-reports/{report.pk}/", {"mobile_minutes": 40}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        report.refresh_from_db()
        self.assertEqual(report.source, "WEB")
        self.assertEqual(response.data["source"], "WEB")

    def test_published_item_day_plan_and_reorder_refresh_after_commit(self):
        self.client.force_authenticate(self.counselor_user)
        with patch("apps.telegram.automation.bot_post_sync") as call:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.patch(f"/api/v1/planning/items/{self.item1.pk}/", {"planned_duration_minutes": 75}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                call.assert_not_called()
            call.assert_called_with("/internal/v1/daily-plan/refresh", self._ids())
            call.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                self.plan.title = "Changed title"
                self.plan.save()
                self.day.save()
            self.assertEqual(call.call_count, 2)
            call.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/api/v1/planning/days/{self.day.pk}/reorder/", {"ordering": [self.item2.pk, self.item1.pk]}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
            call.assert_called_once()

    def test_draft_changes_and_rolled_back_mutations_do_not_refresh(self):
        self.plan.status = Plan.Status.DRAFT
        self.plan.published_at = None
        self.plan.save()
        with patch("apps.telegram.automation.bot_post_sync") as call:
            with self.captureOnCommitCallbacks(execute=True):
                self.item1.planned_duration_minutes = 80
                self.item1.save()
            call.assert_not_called()
            self.plan.status = Plan.Status.PUBLISHED
            self.plan.published_at = timezone.now()
            self.plan.save()
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    self.item1.planned_duration_minutes = 90
                    self.item1.save()
                    transaction.set_rollback(True)
            call.assert_not_called()

    def test_refresh_failure_does_not_rollback_plan_mutation(self):
        with patch("apps.telegram.automation.bot_post_sync", side_effect=RuntimeError("offline")):
            with self.captureOnCommitCallbacks(execute=True):
                self.item1.planned_duration_minutes = 85
                self.item1.save()
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.planned_duration_minutes, 85)

    def test_invalid_telegram_performance_rolls_back_execution_and_source(self):
        execution = transition(self.student_user, self.item1.pk, "quick_complete")
        response = self.client.post("/api/internal/v1/telegram/execution/plan-items/", {
            **self._ids(), "plan_item_id": self.item1.pk, "status": "PARTIAL",
            "actual_duration_minutes": 20, "duration_source": "PLAN",
        }, format="json", **self.svc)
        self.assertEqual(response.status_code, 400, response.data)
        execution.refresh_from_db()
        self.assertEqual(execution.status, "COMPLETED")
        self.assertEqual(execution.source, "WEB")
        self.assertFalse(DailyReport.objects.exists())

    def test_deleted_published_day_still_requests_refresh_of_tracked_message(self):
        with patch("apps.telegram.automation.bot_post_sync", return_value={"status": "refreshed"}) as call:
            with self.captureOnCommitCallbacks(execute=True):
                self.day.delete()
            self.assertTrue(call.called)
