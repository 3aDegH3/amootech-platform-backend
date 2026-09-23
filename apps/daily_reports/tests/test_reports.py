from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Grade, Field, Subject, Chapter, Topic
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.daily_reports.services import report_today
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution

User = get_user_model()
BASE = "/api/v1/daily-reports/"


class DailyReportTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.other_counselor_user = User.objects.create_user("other_counselor", role=User.Role.COUNSELOR)
        self.other_counselor = CounselorProfile.objects.create(user=self.other_counselor_user)
        self.user = User.objects.create_user("student", role=User.Role.STUDENT)
        self.student = StudentProfile.objects.create(user=self.user, counselor=self.counselor)
        self.other_user = User.objects.create_user("other", role=User.Role.STUDENT)
        self.other_student = StudentProfile.objects.create(user=self.other_user, counselor=self.other_counselor)
        grade = Grade.objects.create(name="12")
        field = Field.objects.create(grade=grade, name="Math")
        self.subject = Subject.objects.create(field=field, name="Calculus")
        self.chapter = Chapter.objects.create(subject=self.subject, name="Derivatives")
        self.topic = Topic.objects.create(chapter=self.chapter, name="Optimization")
        self.date = report_today()
        self.plan = Plan.objects.create(
            student=self.student, counselor=self.counselor, start_date=self.date, end_date=self.date,
            status=Plan.Status.PUBLISHED, published_at=timezone.now(),
        )
        self.day = PlanDay.objects.create(plan=self.plan, date=self.date)
        self.study = PlanItem.objects.create(plan_day=self.day, kind="STUDY", subject=self.subject, chapter=self.chapter, topic=self.topic, planned_duration_minutes=90)
        self.test_item = PlanItem.objects.create(plan_day=self.day, kind="TEST", subject=self.subject, planned_duration_minutes=60, test_count=20)
        self.event = PlanItem.objects.create(plan_day=self.day, kind="EVENT", title="Gym")
        self.client.force_authenticate(self.user)

    def open(self):
        return self.client.post(f"{BASE}open/", {"date": str(self.date)}, format="json")

    def test_report_today_uses_tehran_calendar_boundary(self):
        before_midnight_utc = datetime(2026, 9, 22, 20, 29, tzinfo=dt_timezone.utc)
        after_midnight_tehran = datetime(2026, 9, 22, 20, 31, tzinfo=dt_timezone.utc)
        with patch("apps.daily_reports.services.timezone.now", return_value=before_midnight_utc):
            self.assertEqual(str(report_today()), "2026-09-22")
        with patch("apps.daily_reports.services.timezone.now", return_value=after_midnight_tehran):
            self.assertEqual(str(report_today()), "2026-09-23")

    def test_report_list_uses_stable_paginated_contract(self):
        empty = self.client.get(BASE)
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.data, {"count": 0, "next": None, "previous": None, "results": []})
        DailyReport.objects.bulk_create([
            DailyReport(student=self.student, date=self.date - timedelta(days=offset))
            for offset in range(21)
        ])
        first = self.client.get(BASE)
        self.assertEqual(first.data["count"], 21)
        self.assertEqual(len(first.data["results"]), 20)
        self.assertIsNotNone(first.data["next"])
        second = self.client.get(first.data["next"])
        self.assertEqual(len(second.data["results"]), 1)
        self.assertIsNotNone(second.data["previous"])

    def test_open_is_idempotent_and_inherits_plan_and_timer_context(self):
        PlanItemExecution.objects.create(
            student=self.student, plan_item=self.study, status="COMPLETED",
            started_at=timezone.now() - timedelta(minutes=76), accumulated_seconds=76 * 60,
            completed_at=timezone.now(),
        )
        PlanItemExecution.objects.create(student=self.student, plan_item=self.test_item, status="COMPLETED", completed_at=timezone.now())
        first = self.open()
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(self.open().data["id"], first.data["id"])
        self.assertEqual(DailyReport.objects.count(), 1)
        self.assertEqual(DailyReportItem.objects.count(), 3)
        study = next(row for row in first.data["items"] if row["plan_item"] == self.study.pk)
        self.assertEqual(study["subject_name"], "Calculus")
        self.assertEqual(study["topic_name"], "Optimization")
        self.assertEqual(study["planned_duration_minutes"], 90)
        self.assertEqual(study["actual_duration_minutes"], 76)
        self.assertEqual(study["duration_source"], "TIMER")
        test = next(row for row in first.data["items"] if row["plan_item"] == self.test_item.pk)
        self.assertIsNone(test["actual_duration_minutes"])
        self.assertEqual(test["planned_test_count"], 20)
        self.assertEqual(first.data["summary"]["completed_plan_items"], 2)
        self.assertEqual(first.data["summary"]["uncompleted_plan_items"], 0)
        self.assertEqual(first.data["summary"]["total_actual_minutes"], 76)
        self.assertEqual(self.client.get(f"{BASE}{first.data['id']}/").data["items"][0]["actual_duration_minutes"], 76)
        corrected = self.client.put(
            f"{BASE}{first.data['id']}/planned/{self.study.pk}/",
            {"actual_duration_minutes": 80, "duration_source": "MANUAL"}, format="json",
        )
        self.assertEqual(corrected.data["summary"]["total_actual_minutes"], 80)
        self.assertEqual(next(row for row in corrected.data["items"] if row["plan_item"] == self.study.pk)["duration_source"], "MANUAL")
        with self.assertRaises(IntegrityError), transaction.atomic():
            DailyReport.objects.create(student=self.student, date=self.date)

    def test_same_as_plan_test_details_validation_and_daily_edit(self):
        report_id = self.open().data["id"]
        url = f"{BASE}{report_id}/planned/{self.test_item.pk}/"
        updated = self.client.put(url, {"duration_source": "PLAN", "actual_test_count": 20}, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        row = next(row for row in updated.data["items"] if row["plan_item"] == self.test_item.pk)
        self.assertEqual(row["actual_duration_minutes"], 60)
        self.assertEqual(row["duration_source"], "PLAN")
        self.assertEqual(updated.data["summary"]["total_tests"], 20)
        self.assertEqual(self.client.put(url, {"correct_count": 18, "wrong_count": 3}, format="json").status_code, 400)
        self.assertEqual(self.client.put(url, {"correct_count": 14, "wrong_count": 3}, format="json").status_code, 200)
        self.assertEqual(self.client.put(url, {"unanswered_count": 2}, format="json").status_code, 400)
        self.assertEqual(self.client.put(url, {"unanswered_count": 3}, format="json").status_code, 200)
        daily = self.client.patch(f"{BASE}{report_id}/", {"wake_time": "07:00", "mobile_minutes": 45, "self_rating": 17, "note": "Good day"}, format="json")
        self.assertEqual(daily.status_code, 200, daily.data)
        self.assertEqual(daily.data["self_rating"], 17)
        self.assertEqual(self.client.patch(f"{BASE}{report_id}/", {"self_rating": 21}, format="json").status_code, 400)
        self.assertEqual(self.client.get(f"{BASE}{report_id}/").data["wake_time"], "07:00")

    def test_unplanned_activity_and_automatic_totals(self):
        report_id = self.open().data["id"]
        added = self.client.post(f"{BASE}{report_id}/unplanned/", {
            "kind": "TEST", "subject": self.subject.pk, "chapter": self.chapter.pk,
            "topic": self.topic.pk, "actual_duration_minutes": 45, "actual_test_count": 18,
            "correct_count": 14, "wrong_count": 3, "unanswered_count": 1,
        }, format="json")
        self.assertEqual(added.status_code, 201, added.data)
        row = next(item for item in added.data["items"] if item["plan_item"] is None)
        self.assertEqual(row["duration_source"], "MANUAL")
        self.assertEqual(added.data["summary"]["total_actual_minutes"], 45)
        self.assertEqual(added.data["summary"]["total_tests"], 18)
        self.assertEqual(added.data["summary"]["academic_activities"], 1)
        edited = self.client.patch(f"{BASE}{report_id}/unplanned/{row['id']}/", {"actual_duration_minutes": 50}, format="json")
        self.assertEqual(edited.data["summary"]["total_actual_minutes"], 50)
        self.assertEqual(self.client.delete(f"{BASE}{report_id}/unplanned/{row['id']}/").data["summary"]["total_tests"], 0)
        self.assertEqual(self.client.post(f"{BASE}{report_id}/unplanned/", {"kind": "STUDY", "subject": self.subject.pk, "chapter": self.chapter.pk, "topic": self.topic.pk, "actual_duration_minutes": 30, "actual_test_count": 2}, format="json").status_code, 400)

    def test_timed_extra_blocks_conflicts_ordering_and_ownership(self):
        self.study.start_time = "14:00"
        self.study.end_time = "15:00"
        self.study.save()
        report_id = self.open().data["id"]
        endpoint = f"{BASE}{report_id}/unplanned/"
        academic = {"kind": "STUDY", "subject": self.subject.pk, "start_time": "15:00", "end_time": "16:00"}
        self.assertEqual(self.client.post(endpoint, {**academic, "start_time": "14:30"}, format="json").status_code, 400)
        added = self.client.post(endpoint, academic, format="json")
        self.assertEqual(added.status_code, 201, added.data)
        row = next(item for item in added.data["items"] if item["plan_item"] is None)
        self.assertEqual(row["start_time"], "15:00")
        self.assertEqual(row["actual_duration_minutes"], 60)
        self.assertEqual(self.client.patch(f"{endpoint}{row['id']}/", {"note": "جزوه"}, format="json").status_code, 200)
        conflict = self.client.post(endpoint, {"kind": "EVENT", "title": "باشگاه", "start_time": "15:30", "end_time": "16:30"}, format="json")
        self.assertEqual(conflict.status_code, 400)
        self.assertIn("تداخل", str(conflict.data))
        adjacent = self.client.post(endpoint, {"kind": "EVENT", "title": "باشگاه", "start_time": "16:00", "end_time": "17:00"}, format="json")
        self.assertEqual(adjacent.status_code, 201, adjacent.data)
        self.assertEqual(adjacent.data["summary"]["total_actual_minutes"], 60)
        event = next(item for item in adjacent.data["items"] if item["title"] == "باشگاه")
        self.assertEqual(event["actual_duration_minutes"], 60)
        self.assertEqual(self.client.patch(f"{endpoint}{event['id']}/", {"start_time": "15:45"}, format="json").status_code, 400)
        untimed = self.client.post(endpoint, {"kind": "REVIEW", "subject": self.subject.pk, "actual_duration_minutes": 25}, format="json")
        self.assertEqual(untimed.status_code, 201, untimed.data)
        self.assertEqual(untimed.data["summary"]["total_actual_minutes"], 85)
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.patch(f"{endpoint}{row['id']}/", {"note": "x"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(f"{endpoint}{row['id']}/").status_code, 404)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.delete(f"{endpoint}{row['id']}/").status_code, 200)

    def test_manual_status_quick_complete_timer_and_single_actual(self):
        report_id = self.open().data["id"]
        url = f"{BASE}{report_id}/planned/{self.test_item.pk}/"
        partial = self.client.put(url, {"actual_duration_minutes": 45, "actual_test_count": 18, "wrong_count": 4, "completion_status": "PARTIAL"}, format="json")
        self.assertEqual(partial.status_code, 200, partial.data)
        self.assertEqual(PlanItemExecution.objects.get(plan_item=self.test_item).status, "PARTIAL")
        self.assertEqual(partial.data["summary"]["total_actual_minutes"], 45)
        self.assertEqual(partial.data["summary"]["total_tests"], 18)
        test_row = next(item for item in partial.data["items"] if item["plan_item"] == self.test_item.pk)
        self.assertEqual((test_row["correct_count"], test_row["wrong_count"], test_row["unanswered_count"]), (14, 4, None))
        invalid = self.client.put(url, {"actual_test_count": 10, "wrong_count": 11}, format="json")
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("تعداد غلط نمی‌تواند بیشتر", str(invalid.data["wrong_count"]))
        quick = self.client.post(f"/api/v1/planning/items/{self.test_item.pk}/quick-complete/", {}, format="json")
        self.assertEqual(quick.status_code, 200, quick.data)
        self.assertEqual(quick.data["status"], "COMPLETED")
        self.assertEqual(self.client.get(f"{BASE}{report_id}/").data["summary"]["total_actual_minutes"], 45)
        started = self.client.post(f"/api/v1/planning/items/{self.study.pk}/start/", {}, format="json")
        self.assertEqual(started.status_code, 200, started.data)
        execution = PlanItemExecution.objects.get(plan_item=self.study)
        execution.accumulated_seconds = 70 * 60
        execution.current_session_started_at = None
        execution.status = "PAUSED"
        execution.save()
        corrected = self.client.put(f"{BASE}{report_id}/planned/{self.study.pk}/", {"actual_duration_minutes": 80, "completion_status": "COMPLETED"}, format="json")
        self.assertEqual(corrected.status_code, 200, corrected.data)
        self.assertEqual(corrected.data["summary"]["total_actual_minutes"], 125)
        execution.refresh_from_db()
        self.assertEqual(execution.accumulated_seconds, 70 * 60)
        self.assertEqual(execution.status, "COMPLETED")

    def test_close_day_review_active_timer_and_final_summary(self):
        report_id = self.open().data["id"]
        review_url = f"{BASE}{report_id}/close-review/?date={self.date}"
        close_url = f"{BASE}{report_id}/close/"
        review = self.client.get(review_url)
        self.assertEqual({row["id"] for row in review.data["unresolved"]}, {self.study.pk, self.test_item.pk})
        close_payload = {"date": str(self.date), "self_rating": 16, "note": "شیمی کامل نشد."}
        self.assertEqual(self.client.post(close_url, close_payload, format="json").status_code, 400)
        self.client.post(f"/api/v1/planning/items/{self.study.pk}/start/", {}, format="json")
        active = self.client.get(review_url).data
        self.assertEqual(active["active_timer"], self.study.pk)
        self.assertIn("در حال اجرا", str(self.client.post(close_url, close_payload, format="json").data))
        self.client.post(f"/api/v1/planning/items/{self.study.pk}/pause/", {}, format="json")
        self.assertEqual(self.client.put(f"{BASE}{report_id}/planned/{self.study.pk}/", {"actual_duration_minutes": 75, "completion_status": "COMPLETED"}, format="json").status_code, 200)
        self.assertEqual(self.client.put(f"{BASE}{report_id}/planned/{self.test_item.pk}/", {"actual_duration_minutes": 60, "actual_test_count": 18, "wrong_count": 4, "completion_status": "PARTIAL"}, format="json").status_code, 200)
        self.assertEqual(self.client.get(review_url).data["unresolved"], [])
        self.assertEqual(self.client.post(close_url, {**close_payload, "note": ""}, format="json").status_code, 400)
        closed = self.client.post(close_url, close_payload, format="json")
        self.assertEqual(closed.status_code, 200, closed.data)
        self.assertIsNotNone(closed.data["closed_at"])
        self.assertEqual(closed.data["summary"]["total_actual_minutes"], 135)
        self.assertEqual(closed.data["summary"]["planned_minutes"], 150)
        self.assertEqual(closed.data["summary"]["total_tests"], 18)
        self.assertEqual(closed.data["summary"]["planned_tests"], 20)
        self.assertEqual(closed.data["summary"]["completed_plan_items"], 1)
        self.assertEqual(closed.data["summary"]["partial_plan_items"], 1)
        self.assertEqual(closed.data["summary"]["not_done_plan_items"], 0)  # EVENT is outside academic box totals.
        self.assertEqual(closed.data["self_rating"], 16)
        self.assertEqual(closed.data["note"], "شیمی کامل نشد.")
        self.assertEqual(self.client.post(f"/api/v1/planning/items/{self.event.pk}/start/", {}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(f"{BASE}{report_id}/", {"self_rating": 17}, format="json").status_code, 200)

    def test_end_day_is_explicitly_scoped_to_report_date_and_idempotent(self):
        yesterday_plan = Plan.objects.create(
            student=self.student, counselor=self.counselor,
            start_date=self.date - timedelta(days=1), end_date=self.date - timedelta(days=1),
            status=Plan.Status.PUBLISHED, published_at=timezone.now(),
        )
        tomorrow_plan = Plan.objects.create(
            student=self.student, counselor=self.counselor,
            start_date=self.date + timedelta(days=1), end_date=self.date + timedelta(days=1),
            status=Plan.Status.PUBLISHED, published_at=timezone.now(),
        )
        yesterday = PlanItem.objects.create(
            plan_day=PlanDay.objects.create(plan=yesterday_plan, date=self.date - timedelta(days=1)),
            kind="STUDY", subject=self.subject, planned_duration_minutes=30,
        )
        tomorrow = PlanItem.objects.create(
            plan_day=PlanDay.objects.create(plan=tomorrow_plan, date=self.date + timedelta(days=1)),
            kind="STUDY", subject=self.subject, planned_duration_minutes=30,
        )
        report_id = self.open().data["id"]
        review_url = f"{BASE}{report_id}/close-review/"
        self.assertEqual(self.client.get(review_url).status_code, 400)
        self.assertEqual(self.client.get(review_url, {"date": str(self.date + timedelta(days=1))}).status_code, 400)
        review = self.client.get(review_url, {"date": str(self.date)})
        ids = {row["id"] for row in review.data["unresolved"]}
        self.assertEqual(ids, {self.study.pk, self.test_item.pk})
        self.assertNotIn(yesterday.pk, ids)
        self.assertNotIn(tomorrow.pk, ids)

        for item in (self.study, self.test_item):
            self.client.post(f"/api/v1/planning/items/{item.pk}/not-done/", {}, format="json")
        payload = {"date": str(self.date), "self_rating": 18, "note": "روز ثبت شد."}
        first = self.client.post(f"{BASE}{report_id}/close/", payload, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        counts = (DailyReport.objects.count(), DailyReportItem.objects.count(), PlanItemExecution.objects.count())
        second = self.client.post(f"{BASE}{report_id}/close/", {**payload, "self_rating": 1}, format="json")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["self_rating"], 18)
        self.assertEqual((DailyReport.objects.count(), DailyReportItem.objects.count(), PlanItemExecution.objects.count()), counts)

    def test_not_done_is_distinct_and_cannot_hide_recorded_time(self):
        report_id = self.open().data["id"]
        endpoint = f"/api/v1/planning/items/{self.study.pk}/not-done/"
        self.assertEqual(self.client.post(endpoint, {}, format="json").data["status"], "NOT_DONE")
        report = self.client.get(f"{BASE}{report_id}/").data
        self.assertEqual(report["summary"]["not_done_plan_items"], 1)
        self.assertEqual(report["summary"]["uncompleted_plan_items"], 1)
        self.assertEqual(self.client.put(f"{BASE}{report_id}/planned/{self.study.pk}/", {"actual_duration_minutes": 20, "completion_status": "PARTIAL"}, format="json").status_code, 200)
        self.assertEqual(self.client.post(endpoint, {}, format="json").status_code, 400)
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.get(f"{BASE}{report_id}/close-review/").status_code, 404)
        self.assertEqual(self.client.post(f"{BASE}{report_id}/close/", {"self_rating": 16, "note": "x"}, format="json").status_code, 404)

    def test_timer_finish_uses_one_canonical_duration_then_allows_correction(self):
        report_id = self.open().data["id"]
        self.assertEqual(self.client.post(f"/api/v1/planning/items/{self.study.pk}/start/", {}, format="json").status_code, 200)
        execution = PlanItemExecution.objects.get(plan_item=self.study)
        execution.accumulated_seconds = 75 * 60
        execution.current_session_started_at = None
        execution.status = "PAUSED"
        execution.save()
        endpoint = f"{BASE}{report_id}/planned/{self.study.pk}/"
        completed = self.client.put(endpoint, {"completion_status": "COMPLETED"}, format="json")
        self.assertEqual(completed.status_code, 200, completed.data)
        row = next(item for item in completed.data["items"] if item["plan_item"] == self.study.pk)
        self.assertEqual(row["actual_duration_minutes"], 75)
        self.assertEqual(row["duration_source"], "TIMER")
        self.assertEqual(completed.data["summary"]["total_actual_minutes"], 75)
        self.assertEqual(PlanItemExecution.objects.get(plan_item=self.study).accumulated_seconds, 75 * 60)
        corrected = self.client.put(endpoint, {"actual_duration_minutes": 80, "completion_status": "COMPLETED"}, format="json")
        self.assertEqual(corrected.data["summary"]["total_actual_minutes"], 80)
        self.assertEqual(next(item for item in corrected.data["items"] if item["plan_item"] == self.study.pk)["duration_source"], "MANUAL")

    def test_end_day_study_test_review_scenario(self):
        review_item = PlanItem.objects.create(plan_day=self.day, kind="REVIEW", subject=self.subject, planned_duration_minutes=45)
        report_id = self.open().data["id"]
        self.client.post(f"/api/v1/planning/items/{self.study.pk}/start/", {}, format="json")
        execution = PlanItemExecution.objects.get(plan_item=self.study)
        execution.accumulated_seconds = 75 * 60
        execution.current_session_started_at = None
        execution.status = "PAUSED"
        execution.save()
        self.assertEqual(self.client.put(f"{BASE}{report_id}/planned/{self.study.pk}/", {"completion_status": "COMPLETED"}, format="json").status_code, 200)
        self.assertEqual(self.client.put(f"{BASE}{report_id}/planned/{self.test_item.pk}/", {
            "actual_duration_minutes": 60, "actual_test_count": 18, "wrong_count": 4, "completion_status": "COMPLETED",
        }, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/planning/items/{self.event.pk}/not-done/", {}, format="json").status_code, 200)
        pending = self.client.get(f"{BASE}{report_id}/close-review/", {"date": str(self.date)}).data["unresolved"]
        self.assertEqual([item["id"] for item in pending], [review_item.pk])
        self.assertEqual(self.client.put(f"{BASE}{report_id}/planned/{review_item.pk}/", {
            "actual_duration_minutes": 20, "completion_status": "PARTIAL",
        }, format="json").status_code, 200)
        closed = self.client.post(f"{BASE}{report_id}/close/", {"date": str(self.date), "self_rating": 16, "note": "شیمی کامل نشد."}, format="json")
        self.assertEqual(closed.status_code, 200, closed.data)
        self.assertEqual(closed.data["summary"]["total_actual_minutes"], 155)
        self.assertEqual(closed.data["summary"]["total_tests"], 18)
        self.assertEqual(closed.data["summary"]["completed_plan_items"], 2)
        self.assertEqual(closed.data["summary"]["partial_plan_items"], 1)
        self.assertEqual(closed.data["self_rating"], 16)
        self.assertEqual(closed.data["note"], "شیمی کامل نشد.")

    def test_ownership_counselor_scope_and_edit_window(self):
        report_id = self.open().data["id"]
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.get(f"{BASE}{report_id}/").status_code, 404)
        self.assertEqual(self.client.patch(f"{BASE}{report_id}/", {"note": "x"}, format="json").status_code, 404)
        self.client.force_authenticate(self.other_counselor_user)
        self.assertEqual(self.client.get(f"{BASE}{report_id}/").status_code, 404)
        self.client.force_authenticate(self.counselor_user)
        self.assertEqual(self.client.get(f"{BASE}{report_id}/").status_code, 200)
        self.assertEqual(self.client.patch(f"{BASE}{report_id}/", {"note": "x"}, format="json").status_code, 403)
        self.assertEqual(self.client.put(f"{BASE}{report_id}/", {"note": "x"}, format="json").status_code, 403)
        report_page = self.client.get(BASE, {"student": self.student.pk}).data
        self.assertEqual(report_page["count"], 1)
        self.assertEqual(len(report_page["results"]), 1)
        self.client.force_authenticate(self.user)
        future = self.client.post(f"{BASE}open/", {"date": str(self.date + timedelta(days=1))}, format="json")
        self.assertEqual(future.status_code, 400)
        old = self.client.post(f"{BASE}open/", {"date": str(self.date - timedelta(days=31))}, format="json")
        self.assertEqual(old.status_code, 400)
        historical = DailyReport.objects.create(student=self.student, date=self.date - timedelta(days=40))
        DailyReportItem.objects.create(
            report=historical, kind="TEST", title="Historical test", subject=self.subject,
            actual_duration_minutes=30, duration_source="MANUAL", actual_test_count=20,
            correct_count=14, wrong_count=3, unanswered_count=3,
        )
        historical_open = self.client.post(f"{BASE}open/", {"date": str(historical.date)}, format="json")
        self.assertEqual(historical_open.status_code, 200)
        historical_row = historical_open.data["items"][0]
        self.assertEqual((historical_row["correct_count"], historical_row["wrong_count"], historical_row["unanswered_count"]), (14, 3, 3))
        self.assertEqual(self.client.patch(f"{BASE}{historical.pk}/", {"note": "too late"}, format="json").status_code, 400)
        yesterday = self.client.post(f"{BASE}open/", {"date": str(self.date - timedelta(days=1))}, format="json")
        self.assertEqual(yesterday.status_code, 200)
        self.assertEqual(self.client.patch(f"{BASE}{yesterday.data['id']}/", {"note": "Edited later"}, format="json").status_code, 200)

    def test_report_reference_protects_plan_item_delete(self):
        self.open()
        self.client.force_authenticate(self.counselor_user)
        response = self.client.delete(f"/api/v1/planning/items/{self.study.pk}/")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(PlanItem.objects.filter(pk=self.study.pk).exists())

    def test_progress_aggregates_plans_execution_report_and_recent_days(self):
        yesterday = self.date - timedelta(days=1)
        previous = Plan.objects.create(student=self.student, counselor=self.counselor,
            start_date=yesterday, end_date=yesterday, status=Plan.Status.PUBLISHED, published_at=timezone.now())
        previous_day = PlanDay.objects.create(plan=previous, date=yesterday)
        PlanItem.objects.create(plan_day=previous_day, kind="STUDY", subject=self.subject, planned_duration_minutes=30)
        PlanItemExecution.objects.create(student=self.student, plan_item=self.study,
            status="COMPLETED", started_at=timezone.now() - timedelta(minutes=76),
            accumulated_seconds=76 * 60, completed_at=timezone.now())
        PlanItemExecution.objects.create(student=self.student, plan_item=self.test_item,
            status="PARTIAL", completed_at=timezone.now())
        report_id = self.open().data["id"]
        self.client.put(f"{BASE}{report_id}/planned/{self.test_item.pk}/",
            {"actual_duration_minutes": 40, "actual_test_count": 18}, format="json")
        self.client.post(f"{BASE}{report_id}/unplanned/", {
            "kind": "REVIEW", "subject": self.subject.pk, "actual_duration_minutes": 20,
        }, format="json")
        self.client.patch(f"{BASE}{report_id}/", {
            "self_rating": 17, "wake_time": "07:00", "sleep_time": "23:00", "mobile_minutes": 45,
        }, format="json")
        with self.assertNumQueries(4):
            response = self.client.get("/api/v1/student/progress/")
        self.assertEqual(response.status_code, 200, response.data)
        today = response.data["today"]
        self.assertEqual((today["planned_minutes"], today["actual_minutes"]), (150, 136))
        self.assertEqual((today["planned_tests"], today["actual_tests"]), (20, 18))
        self.assertEqual((today["completed"], today["partial"], today["remaining"]), (1, 1, 0))
        self.assertEqual(today["by_subject"], [{"name": "Calculus", "minutes": 136}])
        self.assertEqual((today["self_rating"], today["wake_time"], today["mobile_minutes"]), (17, "07:00", 45))
        self.assertEqual(response.data["recent_days"][1]["planned_minutes"], 30)
        self.assertFalse(response.data["recent_days"][1]["has_report"])

    def test_progress_is_student_only_and_scoped_to_self(self):
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.get("/api/v1/student/progress/").data["today"]["planned_blocks"], 0)
        self.client.force_authenticate(self.counselor_user)
        self.assertEqual(self.client.get("/api/v1/student/progress/").status_code, 403)

    def test_counselor_progress_full_sprint_flow_and_scoping(self):
        self.assertEqual(self.client.get(f"/api/v1/planning/plans/{self.plan.pk}/").status_code, 200)
        base = timezone.now() - timedelta(hours=2)
        action_url = f"/api/v1/planning/items/{self.study.pk}/"
        with patch("apps.planning.execution.timezone.now", return_value=base):
            self.assertEqual(self.client.post(f"{action_url}start/").status_code, 200)
        with patch("apps.planning.execution.timezone.now", return_value=base + timedelta(minutes=30)):
            self.assertEqual(self.client.post(f"{action_url}pause/").status_code, 200)
        with patch("apps.planning.execution.timezone.now", return_value=base + timedelta(minutes=45)):
            self.assertEqual(self.client.post(f"{action_url}resume/").status_code, 200)
        with patch("apps.planning.execution.timezone.now", return_value=base + timedelta(minutes=91)):
            self.assertEqual(self.client.post(f"{action_url}finish/", {"complete": True}, format="json").status_code, 200)
        self.assertEqual(self.open().data["summary"]["total_actual_minutes"], 76)
        report = DailyReport.objects.get(student=self.student, date=self.date)
        self.assertEqual(self.client.put(f"{BASE}{report.pk}/planned/{self.test_item.pk}/",
            {"actual_test_count": 18, "correct_count": 14, "wrong_count": 3, "unanswered_count": 1}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(f"{BASE}{report.pk}/", {"self_rating": 17, "mobile_minutes": 120}, format="json").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/student/progress/").data["today"]["actual_minutes"], 76)

        url = f"/api/v1/counselor/students/{self.student.pk}/progress/"
        self.client.force_authenticate(self.counselor_user)
        with self.assertNumQueries(5):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["today"]["planned_minutes"], 150)
        self.assertEqual(response.data["today"]["actual_minutes"], 76)
        self.assertEqual(response.data["today"]["actual_tests"], 18)
        self.assertEqual(response.data["today"]["completed"], 2)
        study = next(item for item in response.data["planned_items"] if item["id"] == self.study.pk)
        self.assertEqual((study["planned_minutes"], study["actual_minutes"], study["status"]), (90, 76, "COMPLETED"))
        test_item = next(item for item in response.data["planned_items"] if item["id"] == self.test_item.pk)
        self.assertEqual((test_item["actual_tests"], test_item["correct"], test_item["wrong"], test_item["unanswered"]), (18, 14, 3, 1))
        self.assertEqual(response.data["subject_workload"], [{"name": "Calculus", "minutes": 76}])
        self.client.force_authenticate(self.other_counselor_user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_reporting_aggregates_manual_timer_extra_and_topic_exposure(self):
        self.client.post(f"/api/v1/planning/items/{self.study.pk}/start/")
        execution = PlanItemExecution.objects.get(plan_item=self.study)
        execution.accumulated_seconds = 76 * 60
        execution.current_session_started_at = None
        execution.status = PlanItemExecution.Status.COMPLETED
        execution.save(update_fields=("accumulated_seconds", "current_session_started_at", "status"))
        report_id = self.open().data["id"]
        self.client.put(f"{BASE}{report_id}/planned/{self.test_item.pk}/", {
            "actual_duration_minutes": 45, "actual_test_count": 18, "wrong_count": 3,
        }, format="json")
        extra = self.client.post(f"{BASE}{report_id}/unplanned/", {
            "kind": "STUDY", "subject": self.subject.pk, "chapter": self.chapter.pk,
            "topic": self.topic.pk, "actual_duration_minutes": 30,
            "resource": "Video lesson", "note": "Worked examples",
        }, format="json")
        self.assertEqual(extra.status_code, 201, extra.data)
        event = self.client.post(f"{BASE}{report_id}/unplanned/", {
            "kind": "EVENT", "title": "Gym", "start_time": "17:00", "end_time": "18:00", "note": "Training",
        }, format="json")
        self.assertEqual(event.status_code, 201, event.data)
        self.assertEqual(self.client.post(f"{BASE}{report_id}/unplanned/", {
            "kind": "EVENT", "title": "Incomplete event",
        }, format="json").status_code, 400)
        event_row = next(row for row in event.data["items"] if row["title"] == "Gym" and row["plan_item"] is None)
        self.assertEqual(event_row["actual_duration_minutes"], 60)
        params = {"start_date": str(self.date), "end_date": str(self.date), "metric": "study"}
        with self.assertNumQueries(4):
            response = self.client.get("/api/v1/student/reports/", params)
        self.assertEqual(response.status_code, 200, response.data)
        summary = response.data["summary"]
        self.assertEqual((summary["planned_minutes"], summary["actual_minutes"]), (150, 151))
        self.assertEqual((summary["planned_tests"], summary["actual_tests"]), (20, 18))
        self.assertEqual((summary["completed_blocks"], summary["planned_blocks"], summary["completion_percent"]), (2, 2, 100))
        counselor_url = f"/api/v1/counselor/students/{self.student.pk}/reports/"
        self.client.force_authenticate(self.counselor_user)
        counselor_response = self.client.get(counselor_url, params)
        self.assertEqual(counselor_response.status_code, 200, counselor_response.data)
        self.assertEqual(counselor_response.data["summary"], summary)
        self.client.force_authenticate(self.other_counselor_user)
        self.assertEqual(self.client.get(counselor_url, params).status_code, 404)
        self.client.force_authenticate(self.user)
        subject = response.data["subjects"][0]
        self.assertEqual((subject["actual_minutes"], subject["extra_minutes"], subject["wrong_tests"]), (151, 30, 3))
        self.assertEqual(subject["distribution_percent"], 100)
        self.assertEqual((response.data["topic_repetition"][0]["planned"], response.data["topic_repetition"][0]["actual"]), (1, 2))
        self.assertEqual((response.data["topic_repetition"][0]["actual_minutes"], response.data["topic_repetition"][0]["actual_tests"]), (106, 0))
        self.assertEqual(response.data["days"][0]["actual_minutes"], response.data["summary"]["actual_minutes"])
        self.assertEqual(response.data["trend"][0]["value"], 151)
        self.assertEqual(self.client.get("/api/v1/student/reports/", {**params, "metric": "tests"}).data["trend"][0]["value"], 18)
        self.assertEqual(self.client.get("/api/v1/student/reports/", {**params, "metric": "plan"}).data["trend"][0]["value"], 100)
        self.assertEqual(self.client.get(f"{BASE}{report_id}/").data["summary"]["total_actual_minutes"], 151)
        self.client.put(f"{BASE}{report_id}/planned/{self.study.pk}/", {"actual_duration_minutes": 80}, format="json")
        corrected = self.client.get("/api/v1/student/reports/", params)
        self.assertEqual(corrected.data["summary"]["actual_minutes"], 155)

    def test_report_ranges_validation_and_ownership(self):
        url = "/api/v1/student/reports/"
        for days in (1, 7, 30):
            response = self.client.get(url, {"start_date": str(self.date - timedelta(days=days - 1)),
                                             "end_date": str(self.date), "metric": "all"})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(len(response.data["trend"]), days)
            self.assertEqual(response.data["summary"]["planned_minutes"], 150)
        self.assertEqual(self.client.get(url, {"start_date": str(self.date), "end_date": str(self.date - timedelta(days=1))}).status_code, 400)
        self.assertEqual(self.client.get(url, {"start_date": str(self.date - timedelta(days=90)), "end_date": str(self.date)}).status_code, 400)
        self.assertEqual(self.client.get(url, {"start_date": str(self.date), "end_date": str(self.date + timedelta(days=1))}).status_code, 400)
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.get(url, {"start_date": str(self.date), "end_date": str(self.date)}).data["summary"]["planned_minutes"], 0)
        self.client.force_authenticate(self.counselor_user)
        self.assertEqual(self.client.get(url, {"start_date": str(self.date), "end_date": str(self.date)}).status_code, 403)

    def test_reporting_subject_distribution_and_custom_history(self):
        another = Subject.objects.create(field=self.subject.field, name="Physics")
        yesterday = self.date - timedelta(days=1)
        plan = Plan.objects.create(student=self.student, counselor=self.counselor,
            start_date=yesterday, end_date=yesterday, status=Plan.Status.PUBLISHED, published_at=timezone.now())
        day = PlanDay.objects.create(plan=plan, date=yesterday)
        physics = PlanItem.objects.create(plan_day=day, kind="STUDY", subject=another, planned_duration_minutes=60)
        report_id = self.client.post(f"{BASE}open/", {"date": str(yesterday)}, format="json").data["id"]
        self.client.put(f"{BASE}{report_id}/planned/{physics.pk}/", {"actual_duration_minutes": 30}, format="json")
        today_id = self.open().data["id"]
        self.client.put(f"{BASE}{today_id}/planned/{self.study.pk}/", {"actual_duration_minutes": 90}, format="json")
        url = "/api/v1/student/reports/"
        only_yesterday = self.client.get(url, {"start_date": str(yesterday), "end_date": str(yesterday), "metric": "study"})
        self.assertEqual(only_yesterday.data["summary"]["actual_minutes"], 30)
        week = self.client.get(url, {"start_date": str(self.date - timedelta(days=6)), "end_date": str(self.date), "metric": "all"})
        self.assertEqual(week.data["summary"]["actual_minutes"], 120)
        subjects = {item["name"]: item for item in week.data["subjects"]}
        self.assertEqual((subjects["Calculus"]["distribution_percent"], subjects["Physics"]["distribution_percent"]), (75, 25))
        self.assertIsNone(subjects["Physics"]["wrong_tests"])
        self.assertEqual(week.data["summary"]["planned_blocks"], 3)

    def test_reporting_query_count_is_constant_with_many_items(self):
        for index in range(20):
            PlanItem.objects.create(plan_day=self.day, kind="STUDY", subject=self.subject,
                                    planned_duration_minutes=10, ordering=index + 10)
        self.open()
        params = {"start_date": str(self.date - timedelta(days=29)), "end_date": str(self.date), "metric": "all"}
        with self.assertNumQueries(4):
            response = self.client.get("/api/v1/student/reports/", params)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"]["planned_minutes"], 350)
