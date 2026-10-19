from datetime import timedelta
from threading import Event, Thread
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.exams.exceptions import Quiz24Error
from apps.exams.models import Exam, ExamAssignment, ExamAttempt, ExamAnalyticsSync, ExamParticipation
from apps.exams.services.analytics import result_identity, strict_result


class AnalyticsTests(APITestCase):
    def setUp(self):
        self.now = timezone.now()
        self.owner = User.objects.create_user(username="analytics-owner", role="COUNSELOR")
        self.counselor = CounselorProfile.objects.create(user=self.owner)
        self.other_owner = User.objects.create_user(username="analytics-other", role="COUNSELOR")
        self.other_counselor = CounselorProfile.objects.create(user=self.other_owner)
        self.exam = Exam.objects.create(counselor=self.counselor, title="group", provider_exam_code="123", status="PUBLISHED", question_type="1", result_release_at=self.now + timedelta(hours=1))
        self.students, self.assignments = [], []
        for i in range(4):
            student = StudentProfile.objects.create(user=User.objects.create_user(username=f"analytics-s{i}", role="STUDENT", first_name=f"Student{i}"), counselor=self.counselor, mobile=f"0912345678{i}")
            StudentProfile.objects.filter(pk=student.pk).update(mobile_verified_at=self.now, quiz24_sync_status="SYNCED", quiz24_student_id=100+i, quiz24_school_id="42")
            student.refresh_from_db()
            assignment = ExamAssignment.objects.create(exam=self.exam, student=student)
            ExamParticipation.objects.create(assignment=assignment, status="COMPLETED", observed_at=self.now, identity_student_id=student.quiz24_student_id, identity_school_id="42", identity_verified_at=self.now)
            self.students.append(student); self.assignments.append(assignment)
        self.url = f"/api/v1/exams/{self.exam.pk}/analytics/"
        self.client.force_authenticate(self.owner)
        self.school = patch("apps.exams.services.analytics.Quiz24Config").start()
        self.school.return_value.user_id = "42"
        self.provider = patch("apps.exams.services.analytics.Quiz24Service.get_exam_result").start()
        self.addCleanup(patch.stopall)

    def response(self, score, **extra):
        return {"status": 100, "result": {"Score": score, **extra}}

    def sync(self, responses=None):
        self.provider.side_effect = responses or [self.response(80, Balance=6000, Rank=7, RankInClass=3, Correct=8, Wrong=2, NoAnswer=0), self.response(80, Balance=5000, Rank=8), self.response(20, Rank=99), self.response(None)]
        return self.client.post(self.url + "sync/")

    def retry(self):
        ExamAnalyticsSync.objects.filter(exam=self.exam).update(last_attempt_at=self.now - timedelta(minutes=2))

    def test_same_exam_averages_ties_and_provider_rank_distinction(self):
        response = self.sync()
        self.assertEqual(response.status_code, 200, response.data)
        data = self.client.get(self.url).data
        metrics = data["metrics"]
        self.assertEqual([metrics[k] for k in ("assigned", "completed", "valid_scored", "participation_percentage")], [4, 4, 3, 100])
        self.assertEqual(metrics["average_score"], 60)
        self.assertEqual(metrics["median_score"], 80)
        self.assertEqual(metrics["lowest_score"], 20)
        self.assertEqual(metrics["highest_score"], 80)
        self.assertEqual(metrics["average_quiz24_balance"], 5500)
        self.assertEqual(metrics["answer_counts"]["correct_count"], {"total": 8, "average": 8, "contributors": 1})
        self.assertEqual(sum(bin["count"] for bin in metrics["distribution"]), 3)
        self.assertEqual([row["group_rank"] for row in data["results"]], [1, 1, 3, None])
        self.assertEqual(data["results"][0]["provider_rank"], 7)
        self.assertEqual(data["results"][0]["provider_rank_in_class"], 3)
        self.assertTrue(data["ranking"]["provisional"])
        self.assertEqual(data["score_scale"]["unit"], "UNKNOWN")
        self.assertIsNone(data["results"][2]["quiz24_balance"])
        self.assertTrue(data["partial"])

    def test_unknown_units_no_percentage_alias_no_course_score_or_nonfinite_values(self):
        for response in ({"status": 100, "result": {}}, {"status": 100, "result": {"Percent": 70}}, self.response("80%"), self.response(float("nan")), {"status": 100, "result": {"Course": [{"Score": 80}]}}, {"status": 100, "result": [{"Score": 20}, {"Score": 40}]}):
            with self.assertRaises(Quiz24Error):
                strict_result(response, "1")
        metrics, _ = strict_result(self.response(800, Balance=6200, RankInClass=4, Course=[{"Score": 10, "Correct": 1}], ScoreAvg=999, TotalParticipation=999), "1")
        self.assertEqual(metrics["score"], 800)
        self.assertNotIn("ScoreAvg", metrics)
        self.assertIsNone(metrics["correct_count"])

    def test_legacy_missing_and_incomplete_results_are_not_zero_or_ranked(self):
        ExamAttempt.objects.create(exam=self.exam, student=self.students[0], provider_username="legacy", status="COMPLETED", score=100)
        data = self.client.get(self.url).data
        self.assertEqual(data["metrics"]["valid_scored"], 0)
        for field in ("average_score", "median_score", "highest_score", "lowest_score", "average_quiz24_balance"):
            self.assertIsNone(data["metrics"][field])
        self.assertEqual(data["metrics"]["distribution"], [])
        self.assertEqual(data["results"][0]["status"], "NEEDS_VERIFICATION")
        self.sync([self.response(0)] * 4)
        data = self.client.get(self.url).data
        self.assertEqual(data["metrics"]["average_score"], 0)
        self.assertEqual([r["group_rank"] for r in data["results"]], [1, 1, 1, 1])

    def test_repeat_partial_failure_preserves_results_answers_and_unique_attempts(self):
        self.sync([self.response(80, questions=[{"Order": 1, "Answer": "2", "CorrectAnswer": "2"}])] * 4)
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 429)
        self.retry()
        self.sync([Quiz24Error(None, "private failure"), self.response(None), self.response(60), self.response(50)])
        self.assertEqual(ExamAttempt.objects.count(), 4)
        first = ExamAttempt.objects.get(exam=self.exam, student=self.students[0])
        self.assertEqual(first.score, 80)
        self.assertEqual(first.answers.get().answer, "2")
        self.assertEqual(self.client.get(self.url).data["last_error_count"], 2)
        self.assertNotIn("private failure", str(self.client.get(self.url).data))

    def test_access_boundaries_embargo_and_individual_drilldown(self):
        self.sync([self.response(80, questions=[{"Order": 1, "Answer": "2", "CorrectAnswer": "3"}])] * 4)
        detail = f"/api/v1/exams/{self.exam.pk}/result/"
        self.assertEqual(self.client.get(detail, {"student": self.students[1].pk}).data["student"], self.students[1].pk)
        self.assertEqual(self.client.post(detail + f"?student={self.students[1].pk}").status_code, 405)
        for user in (self.other_owner, self.students[0].user, User.objects.create_user(username="analytics-admin", role="ADMIN")):
            self.client.force_authenticate(user)
            self.assertIn(self.client.get(self.url).status_code, (403, 404))
            self.assertIn(self.client.post(self.url + "sync/").status_code, (403, 404))
        self.client.force_authenticate(self.students[0].user)
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.exam.result_release_at = self.now - timedelta(seconds=1)
        self.exam.save()
        self.assertEqual(self.client.get(detail).data["student"], self.students[0].pk)
        self.assertEqual(self.client.get(detail, {"student": self.students[1].pk}).status_code, 404)
        self.assertNotIn("group_rank", self.client.get(detail).data)

    def test_reassignment_identity_changes_and_membership_recompute_ranks(self):
        self.sync([self.response(80), self.response(70), self.response(60), self.response(50)])
        StudentProfile.objects.filter(pk=self.students[0].pk).update(counselor=self.other_counselor)
        data = self.client.get(self.url).data
        self.assertEqual(data["metrics"]["assigned"], 3)
        self.assertEqual(data["results"][0]["group_rank"], 1)
        self.assertEqual(data["results"][0]["student"], self.students[1].pk)
        self.assertEqual(self.client.get(f"/api/v1/exams/{self.exam.pk}/result/", {"student": self.students[0].pk}).status_code, 404)
        StudentProfile.objects.filter(pk=self.students[1].pk).update(quiz24_student_id=999)
        self.assertEqual(self.client.get(self.url).data["metrics"]["valid_scored"], 2)
        self.assignments[2].delete()
        self.assertEqual(self.client.get(self.url).data["metrics"]["assigned"], 2)

    def test_search_filter_ordering_pagination_and_empty_population(self):
        self.sync([self.response(80), self.response(70), self.response(60), self.response(None)])
        self.assertEqual(self.client.get(self.url, {"status": "SCORED"}).data["count"], 3)
        found = self.client.get(self.url, {"q": "Student1"}).data
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["results"][0]["group_rank"], 2)
        self.assertEqual(found["metrics"]["valid_scored"], 3)
        self.assertEqual(self.client.get(self.url, {"ordering": "score_asc"}).data["results"][0]["score"], 60)
        self.assertEqual(self.client.get(self.url, {"ordering": "invalid"}).status_code, 400)
        for i in range(55):
            student = StudentProfile.objects.create(user=User.objects.create_user(username=f"analytics-page-{i}"), counselor=self.counselor)
            ExamAssignment.objects.create(exam=self.exam, student=student)
        self.assertIsNotNone(self.client.get(self.url).data["next"])
        self.assertEqual(self.client.get(self.url, {"page": 2}).status_code, 200)
        self.exam.assignments.all().delete()
        self.assertIsNone(self.client.get(self.url).data["metrics"]["participation_percentage"])

    def test_only_completed_verified_identities_are_requested_and_no_creation(self):
        ExamParticipation.objects.filter(assignment=self.assignments[0]).delete()
        StudentProfile.objects.filter(pk=self.students[1].pk).update(mobile_verified_at=None)
        with patch("apps.exams.services.analytics.Quiz24Service.create_online_exam") as create, patch("apps.exams.services.analytics.Quiz24Service.get_participation_url") as start:
            self.sync([self.response(80), self.response(70)])
            self.assertEqual(self.provider.call_count, 2)
            create.assert_not_called(); start.assert_not_called()
        self.exam.is_personal_practice = True
        self.exam.save()
        calls = self.provider.call_count
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 404)
        self.assertEqual(self.provider.call_count, calls)

    def test_bounded_batches_rotate_and_stale_cached_results_remain_provisional(self):
        with patch("apps.exams.services.analytics.BATCH_SIZE", 2):
            self.sync([self.response(80), self.response(70)])
            data = self.client.get(self.url).data
            self.assertEqual(self.provider.call_count, 2)
            self.assertEqual(data["remaining_in_cycle"], 2)
            self.assertEqual(data["metrics"]["valid_scored"], 2)
            self.retry()
            self.sync([self.response(60), self.response(50)])
            data = self.client.get(self.url).data
            self.assertEqual(data["remaining_in_cycle"], 0)
            self.assertEqual(data["metrics"]["valid_scored"], 4)
        ExamAttempt.objects.filter(exam=self.exam).update(result_verified_at=self.now - timedelta(minutes=6))
        data = self.client.get(self.url).data
        self.assertFalse(data["fresh"])
        self.assertTrue(all(row["stale"] for row in data["results"]))
        self.assertEqual(data["metrics"]["average_score"], 65)
        self.assertTrue(data["ranking"]["provisional"])

    def test_missing_code_and_concurrent_lease_do_not_request_provider(self):
        self.exam.provider_exam_code = None; self.exam.save()
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 409)
        self.exam.provider_exam_code = "123"; self.exam.save()
        ExamAnalyticsSync.objects.create(exam=self.exam, lease_expires_at=self.now + timedelta(minutes=1))
        self.assertEqual(self.client.post(self.url + "sync/").data["code"], "SYNC_IN_PROGRESS")
        self.provider.assert_not_called()


class AnalyticsTransactionTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        AnalyticsTests.setUp(self)

    def test_result_http_outside_transaction_and_fenced_membership_change(self):
        def provider(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            StudentProfile.objects.filter(pk=self.students[0].pk).update(counselor=self.other_counselor)
            return AnalyticsTests.response(self, 80)
        self.provider.side_effect = provider
        response = self.client.post(self.url + "sync/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ExamAttempt.objects.filter(student=self.students[0]).exists())
        self.assertEqual(ExamAttempt.objects.count(), 3)

    def test_individual_result_http_is_outside_transaction_and_respects_changed_embargo(self):
        self.exam.student = self.students[0]
        self.exam.result_release_at = None
        self.exam.save()
        self.client.force_authenticate(self.students[0].user)
        def provider(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            Exam.objects.filter(pk=self.exam.pk).update(result_release_at=timezone.now() + timedelta(hours=1))
            return AnalyticsTests.response(self, 80)
        self.provider.side_effect = provider
        with patch("apps.exams.views.student_quiz24_mobile", return_value=self.students[0].mobile), patch("apps.exams.views.Quiz24Service.get_answersheet", return_value={}):
            response = self.client.post(f"/api/v1/exams/{self.exam.pk}/result/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ExamAttempt.objects.exists())

    def test_inflight_group_response_does_not_overwrite_newer_individual_result(self):
        changed = False
        def provider(*args, **kwargs):
            nonlocal changed
            if not changed:
                ExamAttempt.objects.create(exam=self.exam, student=self.students[0], provider_username=self.students[0].mobile, status="COMPLETED", score=95)
                changed = True
            return AnalyticsTests.response(self, 80)
        self.provider.side_effect = provider
        self.assertEqual(self.client.post(self.url + "sync/").status_code, 200)
        self.assertEqual(ExamAttempt.objects.get(exam=self.exam, student=self.students[0]).score, 95)
        self.assertEqual(self.client.get(self.url).data["last_error_count"], 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_sync_only_one_batch_claim(self):
        entered, release = Event(), Event()
        responses, errors = [], []
        def provider(*args, **kwargs):
            entered.set()
            if not release.wait(5): raise AssertionError("Timeout")
            return AnalyticsTests.response(self, 80)
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
        worker = Thread(target=request); worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.client.post(self.url + "sync/").data["code"], "SYNC_IN_PROGRESS")
        finally:
            release.set(); worker.join(5)
        self.assertFalse(worker.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(self.provider.call_count, 4)
