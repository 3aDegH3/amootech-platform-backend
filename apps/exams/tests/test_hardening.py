"""Integration regressions; all external HTTP and deliveries stay mocked."""
from datetime import timedelta
from threading import Event, Thread
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from apps.accounts.models import User, CounselorProfile, StudentProfile
from apps.exams.exceptions import Quiz24Error
from apps.exams.models import Exam, ExamAttempt, ExamAssignment, ExamAnswer, ExamProviderOperation
from apps.exams.services.quiz24 import Quiz24Service


class ExamFixtures:
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.owner = User.objects.create_user("qa-owner", role="COUNSELOR")
        self.counselor = CounselorProfile.objects.create(user=self.owner)
        self.students = []
        for index in range(3):
            student = StudentProfile.objects.create(user=User.objects.create_user(f"qa-s{index}", role="STUDENT"), counselor=self.counselor, mobile=f"0912345678{index}")
            StudentProfile.objects.filter(pk=student.pk).update(mobile_verified_at=self.now, quiz24_student_id=900 + index, quiz24_school_id="174037", quiz24_sync_status="SYNCED")
            student.refresh_from_db(); self.students.append(student)
        self.exam = Exam.objects.create(counselor=self.counselor, title="QA group", status="PUBLISHED", creation_mode="MANUAL", ever_published=True, provider_exam_id="91", provider_exam_code="321", question_count=4, question_type="1", duration_minutes=10)
        ExamAssignment.objects.bulk_create([ExamAssignment(exam=self.exam, student=student) for student in self.students])
        self.url = f"/api/v1/exams/{self.exam.pk}/"
        self.client = APIClient()
        self.client.force_authenticate(self.students[0].user)
        guard = patch("httpx.Client.post", side_effect=AssertionError("Unexpected live HTTP"))
        guard.start(); self.addCleanup(guard.stop)


@override_settings(QUIZ24_USER_ID="174037", QUIZ24_API_KEY="qa-only-secret", FRONTEND_BASE_URL="http://localhost:3000")
class HardeningTests(ExamFixtures, APITestCase):
    def test_start_rechecks_archive_and_deadline_after_http(self):
        for field, value, expected in (("archived_at", self.now, "MANAGEMENT_RESTRICTED"), ("participation_deadline_at", self.now - timedelta(seconds=1), "PARTICIPATION_DEADLINE_PASSED")):
            Exam.objects.filter(pk=self.exam.pk).update(archived_at=None, participation_deadline_at=None)
            def provider(**kwargs):
                Exam.objects.filter(pk=self.exam.pk).update(**{field: value})
                return "https://example.test/hosted"
            with patch.object(Quiz24Service, "get_participation_url", side_effect=provider):
                response = self.client.post(self.url + "start/")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["code"], expected)
            self.assertNotIn("participation_url", response.data)
        self.assertEqual(self.exam.attempts.count(), 1)
        self.assertIsNone(self.exam.attempts.get().started_at)

    def test_start_rechecks_identity_and_membership_after_http(self):
        def identity_change(**kwargs):
            StudentProfile.objects.filter(pk=self.students[0].pk).update(quiz24_student_id=1234)
            return "https://example.test/hosted"
        with patch.object(Quiz24Service, "get_participation_url", side_effect=identity_change):
            response = self.client.post(self.url + "start/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "START_IDENTITY_CHANGED")
        def removal(**kwargs):
            self.exam.assignments.filter(student=self.students[0]).delete()
            return "https://example.test/hosted"
        with patch.object(Quiz24Service, "get_participation_url", side_effect=removal):
            self.assertEqual(self.client.post(self.url + "start/").status_code, 404)

    def test_completion_during_http_does_not_issue_a_retake_link(self):
        def provider(**kwargs):
            self.exam.attempts.update(status="COMPLETED", score=80)
            return "https://example.test/hosted"
        with patch.object(Quiz24Service, "get_participation_url", side_effect=provider):
            response = self.client.post(self.url + "start/")
        self.assertEqual((response.status_code, response.data["code"]), (409, "ALREADY_COMPLETED"))
        self.assertEqual(self.exam.attempts.get().score, 80)

    def test_completed_attempt_cannot_bypass_unsupported_retake_through_start(self):
        ExamAttempt.objects.create(exam=self.exam, student=self.students[0], status="COMPLETED", score=80, provider_username=self.students[0].mobile)
        with patch.object(Quiz24Service, "get_participation_url") as provider:
            response = self.client.post(self.url + "start/")
        self.assertEqual((response.status_code, response.data["code"]), (409, "ALREADY_COMPLETED"))
        provider.assert_not_called()
        self.assertEqual(self.exam.attempts.get().score, 80)

    def test_start_failure_preserves_evidence_and_releases_only_its_claim(self):
        with patch.object(Quiz24Service, "get_participation_url", side_effect=Quiz24Error(None, "timeout")):
            self.assertEqual(self.client.post(self.url + "start/").status_code, 502)
        attempt = self.exam.attempts.get()
        self.assertIsNone(attempt.started_at)
        self.assertIsNone(attempt.participation_token)
        with patch.object(Quiz24Service, "get_participation_url", return_value="https://example.test/hosted"):
            self.assertEqual(self.client.post(self.url + "start/").status_code, 200)
        self.assertEqual(self.exam.attempts.count(), 1)

    def test_expired_claim_can_recover_and_stale_response_cannot_release_new_claim(self):
        attempt = ExamAttempt.objects.create(exam=self.exam, student=self.students[0], provider_username=self.students[0].mobile, participation_token=uuid4(), participation_lease_until=self.now - timedelta(seconds=1))
        replacement = uuid4()
        def provider(**kwargs):
            ExamAttempt.objects.filter(pk=attempt.pk).update(participation_token=replacement, participation_lease_until=timezone.now() + timedelta(minutes=1))
            return "https://example.test/hosted"
        with patch.object(Quiz24Service, "get_participation_url", side_effect=provider):
            response = self.client.post(self.url + "start/")
        self.assertEqual(response.data["code"], "START_SUPERSEDED")
        attempt.refresh_from_db(); self.assertEqual(attempt.participation_token, replacement)

    def test_incomplete_individual_sync_never_erases_verified_score_or_answers(self):
        good = {"status": 100, "result": {"Score": 80, "Balance": 6000, "questions": [{"Order": 1, "Answer": "2", "CorrectAnswer": "2"}]}}
        with patch.object(Quiz24Service, "get_exam_result", return_value=good), patch.object(Quiz24Service, "get_answersheet", return_value={"status": 100, "result": []}):
            self.assertEqual(self.client.post(self.url + "result/").status_code, 200)
        attempt = self.exam.attempts.get()
        self.assertIsNotNone(attempt.result_verified_at)
        with patch.object(Quiz24Service, "get_exam_result", return_value={"status": 100, "result": {"questions": [{"Order": 1, "Answer": "3"}]}}), patch.object(Quiz24Service, "get_answersheet", return_value={}):
            self.assertEqual(self.client.post(self.url + "result/").status_code, 409)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 80)
        self.assertEqual(attempt.answers.get().answer, "2")
        with patch.object(Quiz24Service, "get_exam_result", return_value={"status": 100, "result": {"Score": 80, "questions": []}}), patch.object(Quiz24Service, "get_answersheet", return_value={}):
            self.assertEqual(self.client.post(self.url + "result/").status_code, 200)
        self.assertEqual(attempt.answers.count(), 1)

    def test_ambiguous_course_scores_and_timezone_free_provider_dates_are_not_invented(self):
        from apps.exams.services.results import parse_result
        for result in ([{"Course": "Math", "Score": 80}, {"Course": "Science", "Score": 60}], [{"Score": 80}, {"Score": 60}]):
            with self.assertRaises(Quiz24Error):
                parse_result({"status": 100, "result": result}, "1")
        metrics, _ = parse_result({"status": 100, "result": {"Score": 80, "startedAt": "2026-10-19T12:00:00", "finishedAt": "2026-10-19T12:30:00+03:30"}}, "1")
        self.assertNotIn("started_at", metrics)
        self.assertEqual(metrics["finished_at"].utcoffset(), timedelta(hours=3, minutes=30))

    def test_endpoint_authorization_matrix(self):
        other = User.objects.create_user("qa-other", role="COUNSELOR")
        CounselorProfile.objects.create(user=other)
        for user, expected in ((other, 404), (self.students[0].user, 403), (None, 401)):
            self.client.force_authenticate(user)
            for action in ("assignments", "schedule", "monitoring", "analytics", "management", "history"):
                with self.subTest(user=str(user), action=action):
                    self.assertEqual(self.client.get(self.url + action + "/").status_code, expected)
            for action in ("assignments", "schedule", "monitoring/sync", "analytics/sync", "management", "edit-questions", "remote-delete", "sync", "publish"):
                method = self.client.patch if action == "schedule" else self.client.post
                self.assertEqual(method(self.url + action + "/", {}, format="json").status_code, expected)
        self.client.force_authenticate(self.students[0].user)
        for student in self.students[1:]:
            self.assertEqual(self.client.get(self.url + f"result/?student={student.pk}").status_code, 404)
        self.assertFalse(self.client.get(self.url).data["result_available"])
        self.assertNotIn("qa-only-secret", str(self.client.get(self.url).data))

    def test_complete_manual_group_lifecycle(self):
        self.client.force_authenticate(self.owner)
        payload = {"student": None, "title": "Integrated", "grade_external_id": 10, "question_type": 1, "duration_minutes": 10, "creation_key": str(uuid4())}
        with patch.object(Quiz24Service, "create_exam", return_value={"status": 100, "examCode": "322", "result": "https://www.quiz24.ir/manage"}) as create:
            first = self.client.post("/api/v1/exams/", payload, format="json")
            self.assertEqual(first.status_code, 201, first.data)
            self.assertEqual(self.client.post("/api/v1/exams/", payload, format="json").data["id"], first.data["id"])
            create.assert_called_once()
        exam = Exam.objects.get(pk=first.data["id"]); url = f"/api/v1/exams/{exam.pk}/"
        with patch.object(Quiz24Service, "get_exam", return_value={"status": 100, "result": {"id": 92, "examCode": 322, "questionCount": 4}}):
            self.assertEqual(self.client.post(url + "sync/").status_code, 200)
        self.assertEqual(self.client.post(url + "assignments/", {"student_ids": [s.pk for s in self.students]}, format="json").status_code, 200)
        start, deadline, release = self.now + timedelta(hours=1), self.now + timedelta(hours=2), self.now + timedelta(hours=3)
        self.assertEqual(self.client.patch(url + "schedule/", {"scheduled_start_at": start.isoformat(), "participation_deadline_at": deadline.isoformat(), "result_release_at": release.isoformat()}, format="json").status_code, 200)
        self.assertEqual(self.client.post(url + "publish/").status_code, 200)
        self.client.force_authenticate(self.students[0].user)
        self.assertEqual(self.client.post(url + "start/").status_code, 409)
        with patch("django.utils.timezone.now", return_value=start), patch.object(Quiz24Service, "get_participation_url", return_value="https://example.test/hosted"):
            self.assertEqual(self.client.post(url + "start/").status_code, 200)
        self.assertEqual(self.client.get(url + "result/").status_code, 403)
        self.client.force_authenticate(self.owner)
        rows = [{"userName": student.mobile, "isStarted": True, "isComplete": True} for student in self.students]
        with patch.object(Quiz24Service, "get_exam_participants", side_effect=[{"status": 100, "result": rows}, {"status": 100, "result": []}]):
            self.assertEqual(self.client.post(url + "monitoring/sync/").status_code, 200)
        with patch.object(Quiz24Service, "get_exam_result", side_effect=[{"status": 100, "result": {"Score": score, "Balance": 6000, "Rank": 9}} for score in (80, 80, 20)]):
            self.assertEqual(self.client.post(url + "analytics/sync/").status_code, 200)
        analytics = self.client.get(url + "analytics/").data
        self.assertEqual([row["group_rank"] for row in analytics["results"]], [1, 1, 3])
        self.assertEqual(analytics["metrics"]["average_score"], 60)
        self.assertEqual(exam.attempts.count(), 3)
        for action in ("archive", "trash", "restore", "unarchive"):
            exam.refresh_from_db()
            self.assertEqual(self.client.post(url + "management/", {"action": action, "expected_updated_at": exam.updated_at.isoformat()}, format="json").status_code, 200)
        self.client.force_authenticate(self.students[0].user)
        with patch("django.utils.timezone.now", return_value=release):
            self.assertEqual(self.client.get(url + "result/").data["score"], 80)
        self.assertEqual(exam.attempts.count(), 3)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(QUIZ24_USER_ID="174037", QUIZ24_API_KEY="qa-only-secret", FRONTEND_BASE_URL="http://localhost:3000")
class HardeningConcurrencyTests(ExamFixtures, TransactionTestCase):
    def run_start_race(self, concurrent):
        entered, release = Event(), Event()
        results, errors = [], []
        def provider(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            if not release.wait(5):
                raise AssertionError("HTTP release timed out")
            return "https://example.test/hosted"
        def worker():
            close_old_connections()
            try:
                client = APIClient(); client.force_authenticate(self.students[0].user)
                results.append(client.post(self.url + "start/"))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        with patch.object(Quiz24Service, "get_participation_url", side_effect=provider) as mocked:
            thread = Thread(target=worker); thread.start()
            try:
                self.assertTrue(entered.wait(5))
                concurrent()
            finally:
                release.set(); thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            mocked.assert_called_once()
        return results[0]

    def test_concurrent_starts_and_assignment_removal_are_fenced(self):
        def concurrent():
            response = self.client.post(self.url + "start/")
            self.assertEqual((response.status_code, response.data["code"]), (409, "START_IN_PROGRESS"))
            self.client.force_authenticate(self.owner)
            response = self.client.delete(self.url + "assignments/", {"student_ids": [self.students[0].pk]}, format="json")
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.run_start_race(concurrent).status_code, 200)
        self.assertEqual(self.exam.attempts.count(), 1)

    def test_archive_during_http_never_returns_link(self):
        def concurrent():
            self.client.force_authenticate(self.owner)
            response = self.client.post(self.url + "management/", {"action": "archive", "expected_updated_at": self.exam.updated_at.isoformat()}, format="json")
            self.assertEqual(response.status_code, 200)
        response = self.run_start_race(concurrent)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("participation_url", response.data)

    @override_settings(QUIZ24_REMOTE_DELETE_ENABLED=True)
    def test_remote_timeout_blocks_concurrent_and_subsequent_destructive_requests(self):
        self.exam.assignments.all().delete()
        self.exam.status = "READY"; self.exam.ever_published = False; self.exam.trashed_at = self.now; self.exam.save()
        self.client.force_authenticate(self.owner)
        entered, release = Event(), Event()
        responses, errors = [], []
        def provider(*args):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            if not release.wait(5):
                raise AssertionError("Delete release timed out")
            raise Quiz24Error(None, "timeout")
        body = {"request_key": str(uuid4()), "expected_updated_at": self.exam.updated_at.isoformat(), "confirm_remote_delete": True, "provider_exam_id": "91", "confirmed_title": self.exam.title}
        def worker():
            close_old_connections()
            try:
                client = APIClient(); client.force_authenticate(self.owner)
                responses.append(client.post(self.url + "remote-delete/", body, format="json"))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        with patch.object(Quiz24Service, "get_exam", return_value={"status": 100, "result": {"id": 91, "examCode": 321, "questionCount": 4}}), patch.object(Quiz24Service, "get_exam_participants", return_value={"status": 100, "result": []}), patch.object(Quiz24Service, "delete_existing_exam", side_effect=provider) as remote:
            thread = Thread(target=worker); thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.client.post(self.url + "remote-delete/", body, format="json").status_code, 409)
                self.exam.refresh_from_db()
                self.assertEqual(self.client.post(self.url + "management/", {"action": "restore", "expected_updated_at": self.exam.updated_at.isoformat()}, format="json").status_code, 400)
            finally:
                release.set(); thread.join(5)
            self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
            self.assertEqual(responses[0].data["state"], "UNKNOWN")
            self.assertEqual(self.client.post(self.url + "remote-delete/", {**body, "request_key": str(uuid4())}, format="json").status_code, 409)
            remote.assert_called_once()

    def test_simultaneous_assignment_additions_remain_unique(self):
        self.exam.assignments.all().delete()
        entered = Event(); results, errors = [], []
        def worker():
            close_old_connections()
            try:
                client = APIClient(); client.force_authenticate(self.owner)
                entered.wait(5)
                results.append(client.post(self.url + "assignments/", {"student_ids": [s.pk for s in self.students]}, format="json").status_code)
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        threads = [Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        entered.set()
        for thread in threads: thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, [200, 200])
        self.assertEqual(self.exam.assignments.count(), 3)
