from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from threading import Event, Thread

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, skipUnlessDBFeature

from rest_framework.test import APITestCase, APIClient

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.exams.models import Exam, ExamAssignment, ExamAttempt


NOW = datetime(2030, 5, 1, 8, tzinfo=dt_timezone.utc)


class SchedulingTests(APITestCase):
    def setUp(self):
        self.clock = patch("django.utils.timezone.now", return_value=NOW)
        self.clock.start()
        self.addCleanup(patch.stopall)
        self.owner = User.objects.create_user(username="schedule-owner", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.owner)
        self.student = StudentProfile.objects.create(user=User.objects.create_user(username="scheduled-student", role=User.Role.STUDENT), counselor=self.counselor)
        self.second = StudentProfile.objects.create(user=User.objects.create_user(username="group-student", role=User.Role.STUDENT), counselor=self.counselor)
        self.exam = Exam.objects.create(student=self.student, counselor=self.counselor, title="scheduled", status="PUBLISHED", provider_exam_code="shared-code", duration_minutes=30)
        ExamAssignment.objects.create(exam=self.exam, student=self.second)
        self.url = f"/api/v1/exams/{self.exam.pk}/"
        self.client.force_authenticate(self.owner)
        self.provider = patch("apps.exams.views.Quiz24Service.get_participation_url", return_value="https://example.test/start").start()
        self.identity = patch("apps.exams.views.student_quiz24_mobile", return_value="09123456789").start()

    def schedule(self, **data):
        return self.client.patch(self.url + "schedule/", data, format="json")

    def test_owner_set_get_clear_explicit_tehran_timezone_and_no_provider_calls(self):
        response = self.schedule(scheduled_start_at="2030-05-01T12:30:00+03:30", participation_deadline_at="2030-05-01T13:30:00+03:30", result_release_at="2030-05-01T14:00:00+03:30")
        self.assertEqual(response.status_code, 200, response.data)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.scheduled_start_at, NOW + timedelta(hours=1))
        self.assertEqual(response.data["availability"]["code"], "UPCOMING")
        self.assertEqual(self.client.get(self.url + "schedule/").status_code, 200)
        self.assertEqual(self.schedule(scheduled_start_at=None, participation_deadline_at=None, result_release_at=None).status_code, 200)
        self.exam.refresh_from_db()
        self.assertIsNone(self.exam.scheduled_start_at)
        self.assertEqual(self.exam.status, "PUBLISHED")
        self.provider.assert_not_called()

    def test_schedule_validation_partial_updates_and_readonly_duration(self):
        for data in (
            {"scheduled_start_at": "2030-05-01T12:30:00"},
            {"scheduled_start_at": "invalid"},
            {"scheduled_start_at": NOW.isoformat()},
            {"participation_deadline_at": (NOW - timedelta(minutes=1)).isoformat()},
            {"duration_minutes": 90},
            {"scheduled_start_at": (NOW + timedelta(hours=2)).isoformat(), "participation_deadline_at": (NOW + timedelta(hours=1)).isoformat()},
            {"scheduled_start_at": (NOW + timedelta(hours=1)).isoformat(), "result_release_at": (NOW + timedelta(hours=1, minutes=29)).isoformat()},
            {"participation_deadline_at": (NOW + timedelta(hours=2)).isoformat(), "result_release_at": (NOW + timedelta(hours=2, minutes=29)).isoformat()},
        ):
            self.assertEqual(self.schedule(**data).status_code, 400, data)
        self.assertEqual(self.schedule(scheduled_start_at=(NOW + timedelta(hours=1)).isoformat()).status_code, 200)
        self.assertEqual(self.schedule(participation_deadline_at=(NOW + timedelta(minutes=59)).isoformat()).status_code, 400)

    def test_only_own_nonpractice_exam_can_be_scheduled(self):
        outsider = User.objects.create_user(username="other-scheduler", role="COUNSELOR")
        CounselorProfile.objects.create(user=outsider)
        self.client.force_authenticate(outsider)
        self.assertEqual(self.schedule(result_release_at=None).status_code, 404)
        for user in (self.student.user, User.objects.create_user(username="schedule-admin", role="ADMIN")):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(self.url + "schedule/").status_code, 403)
            self.assertEqual(self.schedule(result_release_at=None).status_code, 403)
        self.client.force_authenticate(self.owner)
        self.exam.is_personal_practice = True
        self.exam.save()
        self.assertEqual(self.schedule(result_release_at=None).status_code, 404)

    def test_any_attempt_locks_schedule_including_clearing(self):
        self.exam.scheduled_start_at = NOW + timedelta(hours=1)
        self.exam.save()
        ExamAttempt.objects.create(exam=self.exam, student=self.second, provider_username="group")
        self.assertEqual(self.schedule(scheduled_start_at=None).status_code, 409)
        self.assertEqual(self.schedule(result_release_at=(NOW + timedelta(hours=2)).isoformat()).data["code"], "SCHEDULE_LOCKED")
        self.assertTrue(self.client.get(self.url + "schedule/").data["schedule_locked"])
        self.assertEqual(self.schedule(scheduled_start_at=self.exam.scheduled_start_at.isoformat()).status_code, 200)

    def test_future_and_exact_deadline_block_without_provider_identity_calls(self):
        self.exam.scheduled_start_at = NOW + timedelta(microseconds=1)
        self.exam.participation_deadline_at = NOW + timedelta(hours=1)
        self.exam.save()
        self.client.force_authenticate(self.second.user)
        response = self.client.post(self.url + "start/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "EXAM_NOT_STARTED")
        self.exam.scheduled_start_at = NOW - timedelta(hours=1)
        self.exam.participation_deadline_at = NOW
        self.exam.save()
        self.assertEqual(self.client.post(self.url + "start/").data["code"], "PARTICIPATION_DEADLINE_PASSED")
        self.identity.assert_not_called()
        self.provider.assert_not_called()
        self.assertEqual(ExamAttempt.objects.count(), 0)

    def test_exact_start_and_before_deadline_group_access_double_request_single_attempt(self):
        self.exam.scheduled_start_at = NOW
        self.exam.participation_deadline_at = NOW + timedelta(microseconds=1)
        self.exam.save()
        self.client.force_authenticate(self.second.user)
        for _ in range(2):
            self.assertEqual(self.client.post(self.url + "start/").status_code, 200)
        self.assertEqual(self.exam.attempts.count(), 1)
        self.assertEqual(self.exam.attempts.get().started_at, NOW)
        self.provider.assert_called_with(user_name="09123456789", exam_code="shared-code")
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.schedule(scheduled_start_at=None).status_code, 409)

    def test_embargo_hides_scores_answers_and_metadata_but_counselor_can_read(self):
        self.exam.result_release_at = NOW + timedelta(hours=1)
        self.exam.key_file_url = "https://example.test/key"
        self.exam.answer_file_url = "https://example.test/answers"
        self.exam.save()
        attempt = ExamAttempt.objects.create(exam=self.exam, student=self.student, provider_username="first", status="COMPLETED", score=75, rank=1)
        attempt.answers.create(order=1, answer="2", correct_answer="3")
        self.client.force_authenticate(self.student.user)
        with patch("apps.exams.views.Quiz24Service.get_exam_result") as provider:
            for method in (self.client.get, self.client.post):
                response = method(self.url + "result/")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data["code"], "RESULTS_NOT_RELEASED")
                for secret in ("score", "rank", "answers", "key_file_url", "answer_file_url"):
                    self.assertNotIn(secret, response.data)
            provider.assert_not_called()
        detail = self.client.get(self.url).data
        self.assertFalse(detail["result_available"])
        self.assertFalse(detail["availability"]["results_released"])
        self.assertNotIn("key_file_url", detail)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(self.url + "result/").data["score"], 75)
        self.exam.result_release_at = NOW
        self.exam.save()
        self.client.force_authenticate(self.student.user)
        self.assertEqual(self.client.get(self.url + "result/").data["answers"][0]["answer"], "2")
        self.client.force_authenticate(self.second.user)
        self.assertEqual(self.client.get(self.url + "result/").status_code, 404)

    def test_unscheduled_and_practice_still_start_and_closed_does_not(self):
        self.client.force_authenticate(self.student.user)
        self.assertEqual(self.client.post(self.url + "start/").status_code, 200)
        self.exam.is_personal_practice = True
        self.exam.save()
        self.assertEqual(self.client.post(self.url + "start/").status_code, 200)
        self.client.force_authenticate(self.second.user)
        self.assertEqual(self.client.post(self.url + "start/").status_code, 404)
        self.client.force_authenticate(self.student.user)
        self.exam.status = "CLOSED"
        self.exam.save()
        self.assertEqual(self.client.post(self.url + "start/").status_code, 409)
        self.assertEqual(self.client.get(self.url).data["availability"]["code"], "CLOSED")


class ScheduleConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_schedule_rejects_inflight_claim_without_waiting_for_http(self):
        owner = User.objects.create_user(username="concurrent-owner", role="COUNSELOR")
        counselor = CounselorProfile.objects.create(user=owner)
        student = StudentProfile.objects.create(user=User.objects.create_user(username="concurrent-student", role="STUDENT"), counselor=counselor)
        exam = Exam.objects.create(student=student, counselor=counselor, title="concurrent", status="PUBLISHED", provider_exam_code="one", duration_minutes=30)
        entered, release, schedule_done = Event(), Event(), Event()
        responses, errors = {}, []

        def provider(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            if not release.wait(5):
                raise AssertionError("Timed out waiting for concurrent schedule")
            return "https://example.test/start"

        def request(name, user, method, data):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user)
                responses[name] = getattr(client, method)(f"/api/v1/exams/{exam.pk}/{name}/", data, format="json")
            except Exception as error:
                errors.append(error)
            finally:
                if name == "schedule":
                    schedule_done.set()
                connection.close()

        with patch("apps.exams.views.student_quiz24_mobile", return_value="09123456789"), patch("apps.exams.views.Quiz24Service.get_participation_url", side_effect=provider):
            start_thread = Thread(target=request, args=("start", student.user, "post", {}))
            schedule_thread = Thread(target=request, args=("schedule", owner, "patch", {"scheduled_start_at": (datetime.now(dt_timezone.utc) + timedelta(days=1)).isoformat()}))
            start_thread.start()
            try:
                self.assertTrue(entered.wait(5))
                schedule_thread.start()
                self.assertTrue(schedule_done.wait(3))
            finally:
                release.set()
                start_thread.join(5)
                if schedule_thread.ident:
                    schedule_thread.join(5)
            self.assertFalse(start_thread.is_alive())
            self.assertFalse(schedule_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses["start"].status_code, 200)
        self.assertEqual(responses["schedule"].status_code, 409)
        self.assertEqual(exam.attempts.count(), 1)
