from unittest.mock import patch
from uuid import uuid4

from django.db import IntegrityError
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.exams.exceptions import Quiz24Error
from apps.exams.models import Exam, ExamCreation
from apps.exams.serializers import AutomaticCreatedExamSerializer


class AutomaticResponseTests(SimpleTestCase):
    def test_documented_code_and_observed_numeric_code_are_not_exam_ids(self):
        for response in (
            {"status": 100, "result": 3193168},
            {"status": 100, "result": "3193168"},
            {"examCode": "3193168"},
            {"result": {"examCode": "3193168"}},
        ):
            serializer = AutomaticCreatedExamSerializer.from_provider(response)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(serializer.validated_data, {"examCode": "3193168"})

    def test_invalid_and_rejected_responses_do_not_become_codes(self):
        for result in (None, True, False, 0, -1, 1.5, {}, [], ""):
            serializer = AutomaticCreatedExamSerializer.from_provider({"status": 100, "result": result})
            self.assertFalse(serializer.is_valid(), result)
        self.assertFalse(AutomaticCreatedExamSerializer.from_provider({"status": 150, "result": 3193168}).is_valid())


@override_settings(QUIZ24_USER_ID="174037")
class AutomaticCreationTests(APITestCase):
    def setUp(self):
        self.counselor_user = User.objects.create_user(username="builder-counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.student_user = User.objects.create_user(username="builder-student", role=User.Role.STUDENT)
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        self.client.force_authenticate(self.counselor_user)
        self.payload = dict(student=self.student.pk, title="آزمون", grade_external_id=10, course_id=4932, question_type=1, duration_minutes=5, easy_count=1, chapter_ids=[4510], subject_ids=[9061], object_ids=[999], creation_key=str(uuid4()))
        self.counts = patch("apps.exams.views.Quiz24Service.question_count", return_value={"result": {"easyCount": 8, "mediumCount": 287, "hardCount": 82, "veryHardCount": 0}}).start()
        self.create = patch("apps.exams.views.Quiz24Service.create_online_exam", return_value={"status": 100, "message": "با موفقیت انجام شد", "result": 3193168}).start()
        self.addCleanup(patch.stopall)

    def post(self, payload=None):
        return self.client.post("/api/v1/exams/automatic/", payload or self.payload, format="json")

    def test_counselor_numeric_success_persists_owner_code_and_filters_without_fetching_details(self):
        with patch("apps.exams.views.Quiz24Service.get_exam") as details:
            response = self.post()
            details.assert_not_called()
        self.assertEqual(response.status_code, 201, response.data)
        exam = Exam.objects.get()
        self.assertEqual((exam.provider_exam_code, exam.provider_exam_id), ("3193168", None))
        self.assertEqual((exam.student, exam.counselor), (self.student, self.counselor))
        self.assertEqual((exam.status, exam.is_personal_practice, exam.question_count), ("READY", False, 1))
        for key, value in {"grade_id": 10, "course_id": 4932, "chapter_ids": [4510], "subject_ids": [9061], "object_ids": [999]}.items():
            self.assertEqual(self.create.call_args.kwargs[key], value)
            self.assertEqual(self.counts.call_args.kwargs[key], value)
        self.assertNotIn("creation_key", self.create.call_args.kwargs)

    def test_student_numeric_success_preserves_personal_source_and_is_startable(self):
        self.client.force_authenticate(self.student_user)
        payload = {k: v for k, v in self.payload.items() if k != "student"}
        response = self.post(payload)
        self.assertEqual(response.status_code, 201, response.data)
        exam = Exam.objects.get()
        self.assertEqual(exam.student, self.student)
        self.assertEqual(exam.counselor, self.counselor)
        self.assertTrue(exam.is_personal_practice)
        self.assertEqual(exam.status, "PUBLISHED")
        with patch("apps.exams.views.student_quiz24_mobile", return_value="09123456789"), patch("apps.exams.views.Quiz24Service.get_participation_url", return_value="https://example.test/start") as start:
            self.assertEqual(self.client.post(f"/api/v1/exams/{exam.pk}/start/").status_code, 200)
            start.assert_called_once_with(user_name="09123456789", exam_code="3193168")

    def test_replay_returns_same_local_exam_without_second_provider_creation(self):
        first, second = self.post(), self.post()
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(Exam.objects.count(), 1)
        self.create.assert_called_once()
        self.counts.assert_called_once()

    def test_database_failure_after_success_recovers_without_creating_another_provider_exam(self):
        with patch("apps.exams.views.Exam.objects.create", side_effect=IntegrityError("local persistence failure")):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(ExamCreation.objects.get().state, "RECEIVED")
        self.assertEqual(self.post().status_code, 201)
        self.create.assert_called_once()
        self.assertEqual(Exam.objects.count(), 1)

    def test_parser_failure_retains_success_receipt_and_never_recreates(self):
        self.create.return_value = {"status": 100, "result": {"unexpected": 42}}
        self.assertEqual(self.post().status_code, 502)
        self.assertEqual(self.post().status_code, 502)
        self.create.assert_called_once()
        self.assertEqual(ExamCreation.objects.get().response, self.create.return_value)
        self.assertFalse(Exam.objects.exists())

    def test_unknown_network_outcome_is_not_retried(self):
        self.create.side_effect = Quiz24Error(None, "timeout")
        self.assertEqual(self.post().status_code, 502)
        self.assertEqual(self.post().status_code, 409)
        self.create.assert_called_once()
        self.assertEqual(ExamCreation.objects.get().state, "UNKNOWN")

    def test_reusing_key_with_modified_payload_is_rejected_and_key_is_owner_scoped(self):
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(self.post({**self.payload, "easy_count": 2}).status_code, 409)
        self.client.force_authenticate(self.student_user)
        self.assertEqual(self.post({k: v for k, v in self.payload.items() if k != "student"}).status_code, 201)
        self.assertEqual(self.create.call_count, 2)

    def test_requests_without_key_are_also_deduplicated(self):
        payload = {k: v for k, v in self.payload.items() if k != "creation_key"}
        self.assertEqual(self.post(payload).status_code, 201)
        self.assertEqual(self.post(payload).status_code, 200)
        self.create.assert_called_once()

    def test_course_detail_preserves_provider_tree_and_external_ids_for_both_roles(self):
        tree = {"status": 100, "result": [{"key": 4932, "title": "درس", "type": 1, "parentId": None, "children": [{"key": 4510, "title": "فصل", "type": 2, "parentId": 4932, "children": [{"key": 9061, "title": "مبحث", "type": 3, "parentId": 4510, "children": []}]}]}]}
        for user in (self.counselor_user, self.student_user):
            self.client.force_authenticate(user)
            with patch("apps.exams.views.Quiz24Service.course_detail", return_value=tree) as detail:
                response = self.client.post("/api/v1/exams/provider/course-detail/", {"grade_id": 10, "course_id": 4932}, format="json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, tree)
                detail.assert_called_once_with(grade_id=10, course_id=4932, consider_used_question=False)

    def test_concurrent_inflight_creation_cannot_issue_a_second_provider_call(self):
        def reenter(**kwargs):
            self.assertEqual(self.post().status_code, 409)
            return {"status": 100, "result": 3193168}
        self.create.side_effect = reenter
        self.assertEqual(self.post().status_code, 201)
        self.create.assert_called_once()
