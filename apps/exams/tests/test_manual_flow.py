from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlsplit, parse_qs

from django.db import IntegrityError
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User, CounselorProfile, StudentProfile
from apps.exams.models import Exam
from apps.exams.configuration import Quiz24Config
from apps.exams.exceptions import Quiz24Error


@override_settings(QUIZ24_USER_ID="174037", QUIZ24_API_KEY="mock-only", FRONTEND_BASE_URL="http://localhost:3000", QUIZ24_CALLBACK_URL="")
class ManualFlowTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("manual-counselor", role="COUNSELOR")
        self.counselor = CounselorProfile.objects.create(user=self.user)
        self.student = StudentProfile.objects.create(user=User.objects.create_user("manual-student", role="STUDENT"), counselor=self.counselor)
        self.client.force_authenticate(self.user)
        self.payload = dict(student=self.student.pk, title="دستی", grade_external_id=10, question_type=1, duration_minutes=5, creation_key=str(uuid4()))
        self.create = patch("apps.exams.views.Quiz24Service.create_exam", return_value={"status": 100, "examCode": "3193168", "result": "https://www.quiz24.ir/user/ManageExam?id=opaque"}).start()
        self.details = patch("apps.exams.views.Quiz24Service.get_exam", return_value={"status": 100, "result": {"id": 91, "examCode": 3193168, "questionCount": 4, "questionFileName": "https://example.test/q.pdf", "answerFileName": "https://example.test/a.pdf", "keyFileName": "https://example.test/k.pdf"}}).start()
        self.addCleanup(patch.stopall)

    def create_exam(self):
        return self.client.post("/api/v1/exams/", self.payload, format="json")

    def test_callback_bound_to_durable_exam_then_sync_ready_and_publish(self):
        response = self.create_exam()
        self.assertEqual(response.status_code, 201, response.data)
        exam = Exam.objects.get()
        callback = self.create.call_args.kwargs["callback"]
        self.assertEqual(urlsplit(callback).path, "/counselor/exams")
        self.assertEqual(parse_qs(urlsplit(callback).query)["manual_exam"], [str(exam.pk)])
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam.pk}/publish/").status_code, 409)
        response = self.client.post(f"/api/v1/exams/{exam.pk}/sync/")
        self.assertEqual(response.status_code, 200, response.data)
        exam.refresh_from_db()
        self.assertEqual((exam.status, exam.question_count, exam.provider_exam_id), ("READY", 4, "91"))
        self.assertEqual(exam.student_id, self.student.pk)
        self.assertEqual(exam.counselor_id, self.counselor.pk)
        self.assertEqual(exam.answer_file_url, "https://example.test/a.pdf")
        self.assertEqual(exam.key_file_url, "https://example.test/k.pdf")
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam.pk}/publish/").status_code, 200)
        self.create.assert_called_once()

    def test_callback_configuration_query_and_origin_are_preserved(self):
        with override_settings(QUIZ24_CALLBACK_URL="https://front.example.test/counselor/exams?existing=yes"):
            callback = Quiz24Config().manual_callback(42)
            self.assertEqual(urlsplit(callback).netloc, "front.example.test")
            self.assertEqual(parse_qs(urlsplit(callback).query), {"existing": ["yes"], "manual_exam": ["42"]})

    def test_lost_login_and_missing_callback_parameters_can_recover_by_owned_exam(self):
        exam_id = self.create_exam().data["id"]
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.post(f"/api/v1/exams/{exam_id}/sync/").status_code, (401, 403))
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(f"/api/v1/exams/{exam_id}/").status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/sync/").status_code, 200)
        self.create.assert_called_once()

    def test_zero_questions_and_failed_sync_never_mark_ready_and_retry_only_reads(self):
        exam_id = self.create_exam().data["id"]
        self.details.side_effect = Quiz24Error(102, "unavailable")
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/sync/").status_code, 502)
        self.details.side_effect = None
        self.details.return_value = {"status": 100, "result": {"id": 91, "questionCount": 0}}
        response = self.client.post(f"/api/v1/exams/{exam_id}/sync/")
        self.assertEqual(response.data["status"], "DRAFT")
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/publish/").status_code, 409)
        self.create.assert_called_once()

    def test_creation_replay_and_repeat_sync_are_idempotent(self):
        first, second = self.create_exam(), self.create_exam()
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(second.status_code, 200)
        exam = Exam.objects.get()
        self.client.post(f"/api/v1/exams/{exam.pk}/sync/")
        exam.refresh_from_db()
        timestamp = exam.updated_at
        self.client.post(f"/api/v1/exams/{exam.pk}/sync/")
        exam.refresh_from_db()
        self.assertEqual(exam.updated_at, timestamp)
        self.create.assert_called_once()

    def test_uncertain_creation_or_parser_failure_never_creates_another_provider_exam(self):
        self.create.return_value = {"status": 100, "result": "invalid"}
        self.assertEqual(self.create_exam().status_code, 502)
        self.assertEqual(self.create_exam().status_code, 502)
        self.create.assert_called_once()
        self.assertEqual(Exam.objects.count(), 1)

    def test_other_counselor_and_student_cannot_sync_or_publish(self):
        exam_id = self.create_exam().data["id"]
        other = User.objects.create_user("other-manual", role="COUNSELOR")
        CounselorProfile.objects.create(user=other)
        self.client.force_authenticate(other)
        for action in ("sync", "publish"):
            self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/{action}/").status_code, 404)
        self.client.force_authenticate(self.student.user)
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/sync/").status_code, 403)
        self.details.assert_not_called()

    def test_provider_identity_mismatch_is_rejected(self):
        exam_id = self.create_exam().data["id"]
        self.details.return_value["result"]["examCode"] = 888
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/sync/").status_code, 409)
        self.assertEqual(Exam.objects.get().status, "DRAFT")

    def test_persistence_failure_recovers_from_receipt_without_provider_recreation(self):
        original_save = Exam.save
        def fail_after_provider(exam, *args, **kwargs):
            if exam.provider_exam_code:
                raise IntegrityError("local failure after provider success")
            return original_save(exam, *args, **kwargs)
        with patch.object(Exam, "save", fail_after_provider):
            self.assertEqual(self.create_exam().status_code, 503)
        self.assertEqual(self.create_exam().status_code, 201)
        self.assertEqual(Exam.objects.count(), 1)
        self.create.assert_called_once()

    def test_slow_old_sync_cannot_overwrite_a_newer_ready_result(self):
        exam_id = self.create_exam().data["id"]
        fresh = self.details.return_value
        def concurrent_sync(*args, **kwargs):
            with patch("apps.exams.views.Quiz24Service.get_exam", return_value=fresh):
                self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/sync/").status_code, 200)
            return {"status": 100, "result": {"id": 91, "questionCount": 0}}
        self.details.side_effect = concurrent_sync
        response = self.client.post(f"/api/v1/exams/{exam_id}/sync/")
        self.assertEqual(response.data["status"], "READY")
        self.assertEqual(response.data["question_count"], 4)
