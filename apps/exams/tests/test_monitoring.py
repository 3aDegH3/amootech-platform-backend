from datetime import timedelta
from unittest.mock import patch
from threading import Event, Thread

from django.db import close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature

from django.db import connection
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.exams.models import Exam, ExamAssignment, ExamAttempt, ExamMonitoring, ExamParticipation
from apps.exams.exceptions import Quiz24Error
from apps.exams.services.monitoring import identity, parse_participants


class MonitoringTests(APITestCase):
    def setUp(self):
        self.now = timezone.now()
        self.owner = User.objects.create_user(username="monitor-owner", role="COUNSELOR")
        self.counselor = CounselorProfile.objects.create(user=self.owner)
        self.other_owner = User.objects.create_user(username="other-monitor-owner", role="COUNSELOR")
        self.other_counselor = CounselorProfile.objects.create(user=self.other_owner)
        self.students, self.assignments = [], []
        self.exam = Exam.objects.create(counselor=self.counselor, title="group", provider_exam_code="123", status="PUBLISHED", participation_deadline_at=self.now - timedelta(hours=1), result_release_at=self.now + timedelta(hours=1))
        for i in range(4):
            student = StudentProfile.objects.create(user=User.objects.create_user(username=f"monitor-student-{i}", first_name=f"Name{i}", role="STUDENT"), counselor=self.counselor, mobile=f"0912345678{i}")
            StudentProfile.objects.filter(pk=student.pk).update(mobile_verified_at=self.now, quiz24_sync_status="SYNCED", quiz24_student_id=100+i, quiz24_school_id="42")
            student.refresh_from_db()
            self.students.append(student)
            self.assignments.append(ExamAssignment.objects.create(exam=self.exam, student=student))
        self.url = f"/api/v1/exams/{self.exam.pk}/monitoring/"
        self.client.force_authenticate(self.owner)
        self.school = patch("apps.exams.services.monitoring.Quiz24Config").start()
        self.school.return_value.user_id = "42"
        self.provider = patch("apps.exams.services.monitoring.Quiz24Service.get_exam_participants").start()
        self.addCleanup(patch.stopall)

    def rows(self, *statuses):
        return [{"userName": self.students[i].mobile, "fullName": "ignored provider personal data", "isStarted": state != "NOT_STARTED", "isComplete": state == "COMPLETED", "persianDoDate": "unverified calendar format"} for i, state in enumerate(statuses)]

    def sync(self, rows=None):
        self.provider.side_effect = [{"status": 100, "result": rows if rows is not None else self.rows("NOT_STARTED", "IN_PROGRESS", "COMPLETED")}, {"status": 100, "result": []}]
        return self.client.post(self.url + "sync/")

    def allow_retry(self):
        ExamMonitoring.objects.filter(exam=self.exam).update(last_attempt_at=self.now - timedelta(minutes=2))

    def test_mixed_states_paginated_documented_contract_and_no_result_side_effects(self):
        response = self.sync()
        self.assertEqual(response.status_code, 200, response.data)
        summary = response.data["summary"]
        self.assertEqual([summary[k] for k in ("assigned", "NOT_STARTED", "IN_PROGRESS", "COMPLETED", "UNKNOWN", "started")], [4, 1, 1, 1, 1, 2])
        result = self.client.get(self.url).data
        self.assertTrue(result["fresh"])
        self.assertTrue(result["results"][0]["absent"])
        self.assertIsNone(result["results"][1]["started_at"])
        self.assertIsNone(result["results"][2]["completed_at"])
        self.assertEqual(self.provider.call_count, 2)
        self.provider.assert_any_call("123", 1, timeout=15.0)
        self.assertFalse(ExamAttempt.objects.exists())
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.status, "PUBLISHED")
        for row in result["results"]:
            for forbidden in ("score", "rank", "mobile", "userName", "fullName", "persianDoDate"):
                self.assertNotIn(forbidden, row)

    def test_identity_must_be_verified_synced_and_no_display_name_matching(self):
        StudentProfile.objects.filter(pk=self.students[0].pk).update(mobile_verified_at=None)
        StudentProfile.objects.filter(pk=self.students[1].pk).update(quiz24_school_id="different")
        response = self.sync(self.rows("IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS"))
        self.assertEqual(response.status_code, 200)
        result = self.client.get(self.url).data["results"]
        self.assertEqual([r["status"] for r in result], ["UNKNOWN", "UNKNOWN", "IN_PROGRESS", "UNKNOWN"])
        self.assertTrue(result[0]["warnings"])
        self.assertTrue(self.client.get(self.url).data["partial"])
        self.assertNotIn("ignored provider personal data", str(self.client.get(self.url).data))

    def test_integer_username_normalization_and_strict_boolean_contract(self):
        parsed, partial = parse_participants({"status": 100, "result": [{"userName": int(self.students[0].mobile), "isStarted": True, "isComplete": False}, {"userName": self.students[1].mobile, "isStarted": "true", "isComplete": False}]})
        self.assertEqual(parsed[self.students[0].mobile], "IN_PROGRESS")
        self.assertTrue(partial)
        self.assertNotIn(self.students[1].mobile, parsed)

    def test_failure_retains_completed_evidence_and_unknown_for_nonterminal(self):
        self.sync()
        self.allow_retry()
        self.provider.side_effect = Quiz24Error(None, "private provider failure")
        response = self.client.post(self.url + "sync/")
        self.assertEqual(response.status_code, 502)
        result = self.client.get(self.url).data
        self.assertFalse(result["fresh"])
        self.assertEqual([r["status"] for r in result["results"]], ["UNKNOWN", "UNKNOWN", "COMPLETED", "UNKNOWN"])
        self.assertEqual(result["results"][1]["last_known_status"], "IN_PROGRESS")
        self.assertNotIn("private provider failure", str(result))
        self.assertEqual(ExamParticipation.objects.count(), 3)

    def test_empty_or_malformed_page_never_erases_completed_and_missing_is_unknown(self):
        self.sync()
        self.allow_retry()
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(minutes=3)):
            self.sync([])
            result = self.client.get(self.url).data
            self.assertEqual([r["status"] for r in result["results"]], ["UNKNOWN", "UNKNOWN", "COMPLETED", "UNKNOWN"])
        self.allow_retry()
        self.provider.side_effect = [{"status": 100, "result": {"unsupported": []}}]
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 502)
        self.assertEqual(ExamParticipation.objects.filter(status="COMPLETED").count(), 1)

    def test_staleness_and_identity_change_invalidate_nonterminal_observation(self):
        self.sync()
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(minutes=6)):
            data = self.client.get(self.url).data
            self.assertFalse(data["fresh"])
            self.assertEqual(data["summary"]["UNKNOWN"], 3)
        StudentProfile.objects.filter(pk=self.students[1].pk).update(quiz24_student_id=999)
        self.assertEqual(self.client.get(self.url).data["results"][1]["status"], "UNKNOWN")

    def test_unknown_conflicting_and_partial_participants_do_not_leak(self):
        rows = self.rows("IN_PROGRESS") + [{"userName": "09999999999", "fullName": "secret outsider", "isStarted": True, "isComplete": True}]
        self.sync(rows)
        data = self.client.get(self.url).data
        self.assertTrue(data["partial"])
        self.assertEqual(data["summary"]["IN_PROGRESS"], 1)
        self.assertNotIn("secret outsider", str(data))
        self.allow_retry()
        self.sync(self.rows("NOT_STARTED") + self.rows("IN_PROGRESS"))
        self.assertEqual(self.client.get(self.url).data["results"][0]["status"], "UNKNOWN")

    def test_local_pending_is_url_issuance_not_verified_start_and_completed_is_preserved(self):
        first = ExamAttempt.objects.create(exam=self.exam, student=self.students[0], provider_username="unused", started_at=self.now)
        completed = ExamAttempt.objects.create(exam=self.exam, student=self.students[1], provider_username="unused", status="COMPLETED", score=80, finished_at=self.now)
        completed.answers.create(order=1, answer="2")
        data = self.client.get(self.url).data
        self.assertEqual(data["results"][0]["status"], "UNKNOWN")
        self.assertEqual(data["results"][1]["status"], "COMPLETED")
        self.assertIsNone(data["results"][1]["completed_at"])
        self.assertIsNotNone(data["results"][1]["local_attempt"]["finished_at"])
        self.assertTrue(data["results"][0]["warnings"])
        self.assertNotIn("score", data["results"][1])
        self.provider.side_effect = Quiz24Error(None, "timeout")
        self.client.post(self.url + "sync/")
        completed.refresh_from_db(); first.refresh_from_db()
        self.assertEqual(completed.score, 80)
        self.assertEqual(completed.answers.get().answer, "2")
        self.assertEqual(first.status, "PENDING")
        self.client.force_authenticate(self.students[1].user)
        self.assertEqual(self.client.get(f"/api/v1/exams/{self.exam.pk}/result/").status_code, 403)

    def test_permissions_reassignment_and_practice_do_not_call_provider(self):
        for user in (self.students[0].user, self.other_owner, User.objects.create_user(username="monitor-admin", role="ADMIN")):
            self.client.force_authenticate(user)
            for url, method in ((self.url, self.client.get), (self.url + "sync/", self.client.post)):
                self.assertIn(method(url).status_code, (403, 404))
        self.provider.assert_not_called()
        self.client.force_authenticate(self.owner)
        StudentProfile.objects.filter(pk=self.students[0].pk).update(counselor=self.other_counselor)
        self.assertEqual(self.client.get(self.url).data["summary"]["assigned"], 3)
        self.exam.is_personal_practice = True
        self.exam.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 404)
        self.provider.assert_not_called()

    def test_filters_search_pagination_and_empty_assignments(self):
        self.sync()
        self.assertEqual(self.client.get(self.url, {"status": "COMPLETED"}).data["count"], 1)
        self.assertEqual(self.client.get(self.url, {"q": "Name1"}).data["results"][0]["student"], self.students[1].pk)
        self.assertEqual(self.client.get(self.url, {"status": "bad"}).status_code, 400)
        # DRF's configured page size is tested by adding enough local assignments.
        for i in range(55):
            student = StudentProfile.objects.create(user=User.objects.create_user(username=f"page-{i}"), counselor=self.counselor)
            ExamAssignment.objects.create(exam=self.exam, student=student)
        data = self.client.get(self.url).data
        self.assertIsNotNone(data["next"])
        self.assertEqual(self.client.get(self.url, {"page": 2}).status_code, 200)
        self.exam.assignments.all().delete()
        self.assertEqual(self.client.get(self.url).data["summary"]["assigned"], 0)
        self.provider.reset_mock()
        self.assertEqual(self.client.post(self.url + "sync/").data["code"], "NO_ASSIGNMENTS")
        self.provider.assert_not_called()

    def test_repeated_pages_and_late_page_failure_apply_no_partial_updates(self):
        self.provider.return_value = {"status": 100, "result": self.rows("IN_PROGRESS")}
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 502)
        self.assertEqual(self.provider.call_count, 2)
        self.assertFalse(ExamParticipation.objects.exists())
        self.allow_retry()
        self.provider.side_effect = [{"status": 100, "result": self.rows("COMPLETED")}, Quiz24Error(None, "timeout")]
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 502)
        self.assertFalse(ExamParticipation.objects.exists())

    def test_full_name_search_and_syncing_freshness(self):
        self.students[0].user.first_name = "علی"
        self.students[0].user.last_name = "کریمی"
        self.students[0].user.save()
        self.sync()
        self.assertEqual(self.client.get(self.url, {"q": "علی کریمی"}).data["count"], 1)
        ExamMonitoring.objects.filter(exam=self.exam).update(lease_expires_at=timezone.now() + timedelta(seconds=30))
        data = self.client.get(self.url).data
        self.assertFalse(data["fresh"])
        self.assertTrue(data["syncing"])

    def test_cooldown_lease_missing_code_and_missing_identity(self):
        self.sync()
        calls = self.provider.call_count
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 429)
        ExamMonitoring.objects.filter(exam=self.exam).update(lease_expires_at=self.now + timedelta(minutes=1))
        self.assertEqual(self.client.post(self.url + "sync/").data["code"], "SYNC_IN_PROGRESS")
        self.assertEqual(self.provider.call_count, calls)
        self.exam.provider_exam_code = None
        self.exam.save()
        self.assertEqual(self.client.post(self.url + "sync/").data["code"], "MISSING_EXAM_CODE")
        ExamMonitoring.objects.all().delete()
        self.exam.provider_exam_code = "123"
        self.exam.save()
        StudentProfile.objects.filter(pk__in=[s.pk for s in self.students]).update(quiz24_sync_status="NOT_READY")
        self.assertEqual(self.client.post(self.url + "sync/").data["code"], "IDENTITY_NOT_READY")
        self.assertEqual(self.provider.call_count, calls)


class MonitoringTransactionTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        MonitoringTests.setUp(self)

    def test_http_is_outside_transaction_and_exam_id_not_required(self):
        def provider(code, page, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return {"status": 100, "result": MonitoringTests.rows(self, "IN_PROGRESS") if page == 1 else []}
        self.provider.side_effect = provider
        response = self.client.post(self.url + "sync/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(self.exam.provider_exam_id)
        self.assertEqual(ExamAttempt.objects.count(), 0)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_sync_uses_single_lease_and_one_provider_traversal(self):
        entered, release = Event(), Event()
        responses, errors = [], []
        def provider(code, page, **kwargs):
            if page == 1:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Sync release timed out")
            return {"status": 100, "result": MonitoringTests.rows(self, "IN_PROGRESS") if page == 1 else []}
        self.provider.side_effect = provider
        def request():
            close_old_connections()
            try:
                client = APIClient(); client.force_authenticate(self.owner)
                responses.append(client.post(self.url + "sync/"))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        worker = Thread(target=request)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            response = self.client.post(self.url + "sync/")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["code"], "SYNC_IN_PROGRESS")
        finally:
            release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(self.provider.call_count, 2)
