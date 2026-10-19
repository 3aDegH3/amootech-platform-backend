from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.exams.configuration import Quiz24Config
from apps.exams.exceptions import Quiz24Error, quiz24_error_data
from apps.exams.models import Exam, ExamAttempt
from apps.exams.serializers import CreatedQuiz24ExamSerializer, SyncedQuiz24ExamSerializer
from apps.exams.services.quiz24 import Quiz24Service, quiz24_mobile, student_quiz24_mobile
from apps.exams.services.results import safe_raw_result

CONFIG = dict(
    QUIZ24_BASE_URL="https://www.quiz24.ir/api/v1/", QUIZ24_API_KEY="unit-secret-key",
    QUIZ24_USER_ID="42", QUIZ24_CALLBACK_URL="", QUIZ24_STUDENT_CALLBACK_URL="",
    FRONTEND_BASE_URL="https://front.example.test/",
)


@override_settings(**CONFIG)
class ProviderTests(SimpleTestCase):
    def setUp(self):
        self.mock = patch("apps.exams.services.quiz24.httpx.Client")
        self.client = self.mock.start().return_value.__enter__.return_value
        self.addCleanup(self.mock.stop)
        self.respond({"status": 100, "result": []})

    def respond(self, data, status=200):
        self.client.post.return_value = httpx.Response(status, json=data, request=httpx.Request("POST", "https://www.quiz24.ir/api/v1/grades"))

    def test_existing_exam_operations_use_documented_endpoint_and_exact_id(self):
        service = Quiz24Service()
        service.edit_existing_exam("91", "https://front.example.test/counselor/exams")
        args, kwargs = self.client.post.call_args
        self.assertEqual(args[0], CONFIG["QUIZ24_BASE_URL"] + "editExam")
        self.assertEqual(kwargs["json"], {"userId": 42, "examId": 91, "callback": "https://front.example.test/counselor/exams"})
        self.assertNotIn("unit-secret-key", str(kwargs["json"]))
        service.delete_existing_exam("91")
        self.assertEqual(self.client.post.call_args.args[0], CONFIG["QUIZ24_BASE_URL"] + "deleteExam")
        self.assertEqual(self.client.post.call_args.kwargs["json"], {"userId": 42, "examId": 91})

    def test_numeric_exam_codes_are_json_integers_for_documented_endpoints(self):
        self.respond({"status": 100, "result": "https://www.quiz24.ir/run/exam"})
        service = Quiz24Service()
        for method in (service.get_exam_result, service.get_answersheet, service.get_participation_url):
            method("09123456789", "3193168")
            body = self.client.post.call_args.kwargs["json"]
            self.assertEqual(body["examCode"], 3193168)
            self.assertIs(type(body["examCode"]), int)
            self.assertEqual(body["userId"], 42)
            self.assertEqual(body["userName"], "09123456789")

    def test_success_codes(self):
        for code in (100, "100"):
            with self.subTest(code=code):
                self.respond({"status": code, "result": [{"id": 1}]})
                self.assertEqual(Quiz24Service().grades()["result"], [{"id": 1}])

    def test_business_error_and_logs_preserve_safe_message_code(self):
        self.respond({"status": 150, "message": "بسته فعال نیست unit-secret-key"})
        with self.assertLogs("apps.exams.services.quiz24", level="WARNING") as logs:
            with self.assertRaises(Quiz24Error) as caught:
                Quiz24Service().grades()
        data = quiz24_error_data(caught.exception)
        self.assertEqual(data["provider_code"], 150)
        self.assertIn("بسته فعال نیست", data["provider_message"])
        self.assertNotIn("unit-secret-key", str(data) + str(logs.output))
        self.assertEqual(caught.exception.api_status, 409)

    def test_taxonomy_empty_body_and_api_key_header_without_user(self):
        with override_settings(QUIZ24_USER_ID=""):
            for method, endpoint in (("education_periods", "educationPeriods"), ("grades", "grades"), ("courses", "courses")):
                getattr(Quiz24Service(), method)()
                args, kwargs = self.client.post.call_args
                self.assertEqual(args[0], CONFIG["QUIZ24_BASE_URL"] + endpoint)
                self.assertEqual(kwargs["json"], {})
                self.assertEqual(kwargs["headers"], {"X-API-KEY": "unit-secret-key"})

    def test_register_student_uses_school_identity_and_never_logs_credentials(self):
        password = "independent-provider-password"
        Quiz24Service().register_student(mobile="09123456789", first_name="نام", last_name="خانوادگی", password=password)
        body = self.client.post.call_args.kwargs["json"]
        self.assertEqual(body, {"userId": 42, "userName": "09123456789", "name": "نام", "family": "خانوادگی", "password": password, "isActive": True})
        self.respond({"status": 104, "message": password})
        with self.assertLogs("apps.exams.services.quiz24", level="WARNING") as logs:
            with self.assertRaises(Quiz24Error) as caught:
                Quiz24Service().register_student(mobile="09123456789", first_name="نام", last_name="خانوادگی", password=password)
        self.assertNotIn(password, str(logs.output) + str(caught.exception.safe_response))

    def test_course_detail(self):
        Quiz24Service().course_detail(3, 5, True)
        self.assertEqual(self.client.post.call_args.kwargs["json"], {"userId": 42, "gradeId": 3, "courseId": 5, "considerUsedQuestion": True})

    def test_question_count_and_online_payloads(self):
        filters = dict(grade_id=3, course_id=5, question_type=1, chapter_ids=[12, 15], subject_ids=[21], object_ids=[31], from_year=1400, to_year=1404)
        Quiz24Service().question_count(**filters)
        body = self.client.post.call_args.kwargs["json"]
        self.assertEqual(body["chapterIds"], "12,15")
        self.assertEqual(body["subjectIds"], "21")
        self.assertEqual(body["objectIds"], "31")
        Quiz24Service().create_online_exam(**filters, title="آزمون", easy_count=2, medium_count=0, hard_count=0, very_hard_count=0, duration_minutes=20)
        body = self.client.post.call_args.kwargs["json"]
        self.assertEqual(body["callback"], "https://front.example.test/counselor/exams")
        self.assertIs(type(body["userId"]), int)
        self.assertEqual(body["userId"], 42)
        self.assertEqual(body["chapterIds"], "12,15")
        self.assertFalse(body["considerUsedQuestion"])

    def test_hierarchy_and_optional_omission(self):
        with self.assertRaises(Quiz24Error):
            Quiz24Service().question_count(grade_id=1, course_id=2, question_type=1, subject_ids=[2])
        self.client.post.assert_not_called()
        Quiz24Service().question_count(grade_id=1, course_id=2, question_type=1)
        body = self.client.post.call_args.kwargs["json"]
        self.assertNotIn("chapterIds", body)
        self.assertNotIn("fromYear", body)

    def test_manual_callback_fallback_and_explicit_priority(self):
        Quiz24Service().create_exam(title="آزمون", grade_external_id=3, question_type=1, duration_minutes=30, has_negative_score=False)
        body = self.client.post.call_args.kwargs["json"]
        self.assertEqual(body["callback"], "https://front.example.test/counselor/exams")
        self.assertEqual(body["type"], 1)
        self.assertEqual(Quiz24Config().callback(student=True), "https://front.example.test/student/exams")
        with override_settings(QUIZ24_CALLBACK_URL="https://other.example.test/return"):
            self.assertEqual(Quiz24Config().callback(), "https://other.example.test/return")

    def test_invalid_callbacks(self):
        for value in ("/return", "javascript:alert(1)", "ftp://example.test/cb"):
            with override_settings(QUIZ24_CALLBACK_URL=value):
                with self.assertRaises(Quiz24Error) as caught:
                    Quiz24Service().create_exam(title="x", grade_external_id=1, question_type=1, duration_minutes=10, has_negative_score=False)
                self.assertEqual(caught.exception.api_status, 400)
                self.assertIn("آدرس بازگشت", caught.exception.message)
        with override_settings(FRONTEND_BASE_URL=""):
            with self.assertRaises(Quiz24Error):
                Quiz24Config().callback()
        self.client.post.assert_not_called()

    def test_invalid_config_and_base_join(self):
        for setting in (dict(QUIZ24_API_KEY=""), dict(QUIZ24_BASE_URL="broken"), dict(QUIZ24_USER_ID="not-numeric"), dict(QUIZ24_USER_ID="9" * 5000)):
            with override_settings(**setting), self.assertRaises(Quiz24Error) as caught:
                Quiz24Service().get_exam("exam")
            self.assertEqual(caught.exception.api_status, 400)
        with override_settings(QUIZ24_BASE_URL="https://www.quiz24.ir/api/v1/api/v1"):
            Quiz24Service().grades()
            self.assertEqual(self.client.post.call_args.args[0], "https://www.quiz24.ir/api/v1/grades")

    def test_network_errors(self):
        for error, expected in ((httpx.ReadTimeout("timeout"), "پاسخی از Quiz24 دریافت نشد."), (httpx.ConnectError("connection"), "اتصال به Quiz24 برقرار نشد.")):
            self.client.post.side_effect = error
            with self.assertRaises(Quiz24Error) as caught:
                Quiz24Service().grades()
            self.assertEqual(caught.exception.message, expected)
            self.assertEqual(caught.exception.api_status, 502)

    def test_invalid_json_and_structure(self):
        self.client.post.return_value = httpx.Response(502, text="not JSON", request=httpx.Request("POST", "https://example.test"))
        with self.assertRaises(Quiz24Error) as caught:
            Quiz24Service().grades()
        self.assertEqual(caught.exception.message, "پاسخ نامعتبر از Quiz24 دریافت شد.")
        self.respond({"result": []})
        with self.assertRaises(Quiz24Error) as caught:
            Quiz24Service().grades()
        self.assertEqual(caught.exception.message, "ساختار پاسخ Quiz24 قابل پردازش نیست.")
        self.respond([])
        with self.assertRaises(Quiz24Error) as caught:
            Quiz24Service().grades()
        self.assertEqual(caught.exception.message, "ساختار پاسخ Quiz24 قابل پردازش نیست.")

    def test_invalid_proxy_configuration_is_safe(self):
        self.client.post.side_effect = ValueError("Unknown scheme for proxy URL with-sensitive-details")
        with self.assertRaises(Quiz24Error) as caught:
            Quiz24Service().grades()
        self.assertEqual(caught.exception.api_status, 400)
        self.assertIn("پراکسی", caught.exception.message)
        self.assertNotIn("sensitive-details", caught.exception.message)

    def test_mobile_resolution_never_uses_arbitrary_username(self):
        student = SimpleNamespace(user=SimpleNamespace(username="demo-student"), mobile="۰۹۱۲۳۴۵۶۷۸۹", mobile_verified_at=True, quiz24_sync_status="SYNCED", quiz24_student_id=99, quiz24_school_id="42")
        self.assertEqual(student_quiz24_mobile(student), "09123456789")
        self.respond({"status": 100, "result": "https://www.quiz24.ir/run/exam"})
        Quiz24Service().get_participation_url(student_quiz24_mobile(student), "code")
        body = self.client.post.call_args.kwargs["json"]
        self.assertEqual(body["userName"], "09123456789")
        self.assertEqual(body["callback"], "https://front.example.test/student/exams")
        self.assertFalse(body["isEditAnswersheet"])
        for value in (None, "demo-student", "+989123456789", "0912"):
            with self.assertRaises(Quiz24Error):
                quiz24_mobile(value)
        with self.assertRaises(Quiz24Error):
            student_quiz24_mobile(SimpleNamespace(user=SimpleNamespace(username="09123456789")))

    def test_response_mappings(self):
        serializer = CreatedQuiz24ExamSerializer.from_provider({"status": "100", "result": {"examCode": "code", "result": "https://www.quiz24.ir/ManageExam/code"}})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer = SyncedQuiz24ExamSerializer.from_provider({"status": 100, "result": {"id": 11, "questionCount": 3, "fileName": "generic.pdf", "questionFileName": "questions.pdf", "answerFileName": "answers.pdf", "keyFileName": "key.pdf"}})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["question_file_url"], "questions.pdf")
        self.assertEqual(serializer.validated_data["answer_file_url"], "answers.pdf")
        self.assertEqual(serializer.validated_data["key_file_url"], "key.pdf")
        serializer = SyncedQuiz24ExamSerializer.from_provider({"id": 11, "questionCount": 3, "fileName": "generic.pdf"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("question_file_url", serializer.validated_data)
        safe = safe_raw_result({"result": {"score": 50, "questions": [{"Order": 1, "Answer": "2", "CorrectAnswer": "2", "QuestionText": "raw content", "QuestionId": 123}]}})
        self.assertEqual(safe["result"]["score"], 50)
        self.assertEqual(safe["result"]["questions"], [{"Order": 1, "Answer": "2", "CorrectAnswer": "2"}])


@override_settings(**CONFIG)
class ExamAccessTests(APITestCase):
    def setUp(self):
        self.counselor_user = User.objects.create_user(username="counselor-a", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        other_user = User.objects.create_user(username="counselor-b", role=User.Role.COUNSELOR)
        self.other = CounselorProfile.objects.create(user=other_user)
        self.student_user = User.objects.create_user(username="student-a", role=User.Role.STUDENT)
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        self.other_student = StudentProfile.objects.create(user=User.objects.create_user(username="student-b", role=User.Role.STUDENT), counselor=self.other)
        self.exam = Exam.objects.create(student=self.student, counselor=self.counselor, title="آزمون", status=Exam.Status.PUBLISHED, provider_exam_code="code", question_count=3)

    def test_counselor_ownership_before_provider_call(self):
        self.client.force_authenticate(self.counselor_user)
        with patch("apps.exams.views.Quiz24Service.create_exam") as provider:
            response = self.client.post("/api/v1/exams/", dict(student=self.other_student.pk, title="test", grade_external_id=1, question_type=1, duration_minutes=10), format="json")
            self.assertEqual(response.status_code, 400)
            provider.assert_not_called()
        other_exam = Exam.objects.create(student=self.other_student, counselor=self.other, title="other")
        for action in ("sync", "publish", "result"):
            self.assertEqual(self.client.post(f"/api/v1/exams/{other_exam.pk}/{action}/").status_code, 404)

    def test_student_only_own_published_and_no_management(self):
        Exam.objects.create(student=self.student, counselor=self.counselor, title="draft")
        Exam.objects.create(student=self.other_student, counselor=self.other, title="other", status=Exam.Status.PUBLISHED)
        self.client.force_authenticate(self.student_user)
        response = self.client.get("/api/v1/exams/")
        self.assertEqual([row["id"] for row in response.data["results"]], [self.exam.pk])
        self.assertNotIn("manage_url", response.data["results"][0])
        for action in ("sync", "publish"):
            self.assertEqual(self.client.post(f"/api/v1/exams/{self.exam.pk}/{action}/").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/exams/", {}, format="json").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/exams/automatic/", {}, format="json").status_code, 400)

    def test_missing_mobile_does_not_call_provider(self):
        self.client.force_authenticate(self.student_user)
        with patch("apps.exams.views.Quiz24Service.get_participation_url") as provider:
            response = self.client.post(f"/api/v1/exams/{self.exam.pk}/start/")
            self.assertEqual(response.status_code, 409)
            self.assertIn("موبایل", response.data["detail"])
            self.assertIn("تأیید", response.data["detail"])
            provider.assert_not_called()

    def test_status_is_protected_and_secret_free(self):
        self.client.force_authenticate(self.counselor_user)
        response = self.client.get("/api/v1/exams/provider/status/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["configured"])
        self.assertNotIn("unit-secret-key", str(response.data))
        self.assertNotIn("42", str(response.data))
        self.client.force_authenticate(self.student_user)
        self.assertEqual(self.client.get("/api/v1/exams/provider/status/").status_code, 403)

    def test_result_persistence_survives_answersheet_failure_and_is_idempotent(self):
        self.client.force_authenticate(self.counselor_user)
        with patch("apps.exams.views.student_quiz24_mobile", return_value="09123456789"), patch("apps.exams.views.Quiz24Service.get_exam_result", return_value={"status": 100, "result": {"score": 75, "correctCount": 1, "questions": [{"Order": 1, "Answer": "2", "CorrectAnswer": "2"}]}}), patch("apps.exams.views.Quiz24Service.get_answersheet", side_effect=Quiz24Error(150, "unavailable")):
            for _ in range(2):
                response = self.client.post(f"/api/v1/exams/{self.exam.pk}/result/")
                self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ExamAttempt.objects.count(), 1)
        self.assertEqual(ExamAttempt.objects.get().answers.count(), 1)
        self.assertEqual(response.data["score"], 75)
        self.assertEqual(self.client.get(f"/api/v1/exams/{self.exam.pk}/result/").status_code, 200)

    def test_manual_create_sync_publish_lifecycle(self):
        self.client.force_authenticate(self.counselor_user)
        with patch("apps.exams.views.Quiz24Service.create_exam", return_value={"status": 100, "result": "https://www.quiz24.ir/ManageExam/code", "examCode": "manual-code"}):
            response = self.client.post("/api/v1/exams/", dict(student=self.student.pk, title="new exam", grade_external_id=1, question_type=1, duration_minutes=20), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        exam_id = response.data["id"]
        self.assertEqual(response.data["status"], "DRAFT")
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/publish/").status_code, 409)
        with patch("apps.exams.views.Quiz24Service.get_exam", return_value={"status": 100, "result": {"id": 91, "questionCount": 4, "questionFileName": "questions.pdf", "answerFileName": "answers.pdf", "keyFileName": "key.pdf", "student": self.other_student.pk}}):
            response = self.client.post(f"/api/v1/exams/{exam_id}/sync/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["student"], self.student.pk)
        self.assertEqual(response.data["status"], "READY")
        self.assertEqual(self.client.post(f"/api/v1/exams/{exam_id}/publish/").status_code, 200)

    def test_automatic_inventory_and_subscription_failures(self):
        self.client.force_authenticate(self.counselor_user)
        payload = dict(student=self.student.pk, title="automatic", grade_external_id=1, course_id=2, question_type=1, duration_minutes=20, easy_count=3)
        with patch("apps.exams.views.Quiz24Service.question_count", return_value={"status": 100, "result": {"easyCount": 2, "mediumCount": 0, "hardCount": 0, "veryHardCount": 0}}), patch("apps.exams.views.Quiz24Service.create_online_exam") as create:
            response = self.client.post("/api/v1/exams/automatic/", payload, format="json")
            self.assertEqual(response.status_code, 400)
            create.assert_not_called()
        with patch("apps.exams.views.Quiz24Service.question_count", side_effect=Quiz24Error(150, "package unavailable", api_status=409)):
            response = self.client.post("/api/v1/exams/automatic/", payload, format="json")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["provider_code"], 150)
            self.assertIn("بسته بانک سوال", response.data["detail"])

    def test_result_flag_is_scoped_to_assigned_student(self):
        ExamAttempt.objects.create(exam=self.exam, student=self.other_student, provider_username="09123456789", status=ExamAttempt.Status.COMPLETED)
        self.client.force_authenticate(self.student_user)
        response = self.client.get("/api/v1/exams/")
        self.assertFalse(response.data["results"][0]["result_available"])
        self.assertEqual(self.client.get(f"/api/v1/exams/{self.exam.pk}/result/").status_code, 404)
