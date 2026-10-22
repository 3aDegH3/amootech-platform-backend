from unittest.mock import patch

from rest_framework.test import APITestCase

from django.core.cache import cache
from apps.academics.models import Grade
from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.exams.exceptions import Quiz24Error
from apps.exams.models import Exam, ExamAttempt


class StudentPracticeTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="practice-student", role=User.Role.STUDENT)
        cache.clear()
        grade = Grade.objects.create(name="هفتم", number=7)
        self.student = StudentProfile.objects.create(user=self.user, grade=grade)
        patch("apps.exams.views.Quiz24Service.grades", return_value={"result":[{"id":1,"name":"هفتم"}]}).start()
        patch("apps.exams.views.Quiz24Service.courses", return_value={"result":[{"id":2,"name":"علوم","gradeId":1}]}).start()
        self.other_user = User.objects.create_user(username="other-student", role=User.Role.STUDENT)
        self.other = StudentProfile.objects.create(user=self.other_user)
        self.client.force_authenticate(self.user)
        self.payload = dict(title="تمرین فیزیک", grade_external_id=1, course_id=2, question_type=1, duration_minutes=20, easy_count=3)
        self.counts = patch("apps.exams.views.Quiz24Service.question_count", return_value={"result": {"easyCount": 4, "mediumCount": 0, "hardCount": 0, "veryHardCount": 0}}).start()
        self.create = patch("apps.exams.views.Quiz24Service.create_online_exam", return_value={"examCode": "practice-code"}).start()
        self.addCleanup(patch.stopall)

    def test_create_uses_authenticated_owner_without_counselor_and_is_startable(self):
        response = self.client.post("/api/v1/exams/automatic/", self.payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        exam = Exam.objects.get(pk=response.data["id"])
        self.assertEqual(exam.student, self.student)
        self.assertIsNone(exam.counselor)
        self.assertTrue(exam.is_personal_practice)
        self.assertEqual(exam.status, Exam.Status.PUBLISHED)
        self.assertTrue(response.data["is_personal_practice"])
        self.assertNotIn("provider_exam_code", response.data)
        rows = self.client.get("/api/v1/exams/").data["results"]
        self.assertEqual([row["id"] for row in rows], [exam.pk])
        self.assertTrue(rows[0]["is_personal_practice"])
        with patch("apps.exams.views.student_quiz24_mobile", return_value="09123456789"), patch("apps.exams.views.Quiz24Service.get_participation_url", return_value="https://example.test/exam") as start:
            self.assertEqual(self.client.post(f"/api/v1/exams/{exam.pk}/start/").status_code, 200)
            start.assert_called_once_with(user_name="09123456789", exam_code="practice-code")

    def test_student_cannot_supply_any_owner_even_self_or_null(self):
        for owner in (self.other.pk, self.student.pk, None):
            for key in ("student", "student_id", "counselor", "counselor_id"):
                with self.subTest(key=key, owner=owner):
                    response = self.client.post("/api/v1/exams/automatic/", {**self.payload, key: owner}, format="json")
                    self.assertEqual(response.status_code, 400)
        self.counts.assert_not_called()
        self.create.assert_not_called()
        self.assertFalse(Exam.objects.exists())

    def test_assigned_counselor_is_resolved_without_accepting_frontend_id(self):
        counselor_user = User.objects.create_user(username="practice-counselor", role=User.Role.COUNSELOR)
        counselor = CounselorProfile.objects.create(user=counselor_user)
        self.student.counselor = counselor
        self.student.save()
        response = self.client.post("/api/v1/exams/automatic/", self.payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Exam.objects.get().counselor, counselor)

    def test_other_students_cannot_see_start_or_fetch_practice_results(self):
        response = self.client.post("/api/v1/exams/automatic/", self.payload, format="json")
        exam_id = response.data["id"]
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.get("/api/v1/exams/").data["results"], [])
        with patch("apps.exams.views.Quiz24Service.get_participation_url") as start, patch("apps.exams.views.Quiz24Service.get_exam_result") as result:
            for path, method in (("", "get"), ("start/", "post"), ("result/", "get"), ("result/", "post")):
                self.assertEqual(getattr(self.client, method)(f"/api/v1/exams/{exam_id}/{path}").status_code, 404)
            start.assert_not_called()
            result.assert_not_called()

    def test_closed_own_exam_and_result_remain_visible_but_not_startable(self):
        exam = Exam.objects.create(student=self.student, title="گذشته", status=Exam.Status.CLOSED, is_personal_practice=True)
        ExamAttempt.objects.create(exam=exam, student=self.student, provider_username="09123456789", status=ExamAttempt.Status.COMPLETED, score=80)
        rows = self.client.get("/api/v1/exams/").data["results"]
        self.assertTrue(rows[0]["result_available"])
        self.assertEqual(self.client.get(f"/api/v1/exams/{exam.pk}/result/").data["score"], 80)
        with patch("apps.exams.views.Quiz24Service.get_participation_url") as start:
            self.assertEqual(self.client.post(f"/api/v1/exams/{exam.pk}/start/").status_code, 409)
            start.assert_not_called()

    def test_taxonomy_access_is_limited_to_builder_endpoints(self):
        for path, method, payload, provider_method, response in (
            ("grades", "get", None, "grades", {"result": [{"id":1,"name":"هفتم"}]}),
            ("courses", "get", None, "courses", {"result": [{"id":2,"name":"علوم","gradeId":1}]}),
            ("course-detail", "post", {"grade_id": 1, "course_id": 2}, "course_detail", {"result": []}),
        ):
            with patch(f"apps.exams.views.Quiz24Service.{provider_method}", return_value=response):
                self.assertEqual(getattr(self.client, method)(f"/api/v1/exams/provider/{path}/", payload, format="json").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/exams/provider/question-count/", {"grade_id": 1, "course_id": 2, "question_type": 1}, format="json").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/exams/provider/status/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/exams/provider/education-periods/").status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get("/api/v1/exams/provider/grades/").status_code, (401, 403))
        self.assertIn(self.client.post("/api/v1/exams/automatic/", self.payload, format="json").status_code, (401, 403))

    def test_invalid_counts_hierarchy_and_subscription_errors_do_not_create(self):
        for change in ({"easy_count": 5}, {"easy_count": 0}, {"easy_count": -1}, {"subject_ids": [2]}):
            self.assertEqual(self.client.post("/api/v1/exams/automatic/", {**self.payload, **change}, format="json").status_code, 400)
        self.create.assert_not_called()
        self.counts.side_effect = Quiz24Error(150, "package unavailable", api_status=409)
        response = self.client.post("/api/v1/exams/automatic/", self.payload, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertIn("detail", response.data)
        self.assertFalse(Exam.objects.exists())

    def test_student_still_cannot_manage_or_create_manual_exams(self):
        response = self.client.post("/api/v1/exams/automatic/", self.payload, format="json")
        for action in ("sync", "publish"):
            self.assertEqual(self.client.post(f'/api/v1/exams/{response.data["id"]}/{action}/').status_code, 403)
        self.assertEqual(self.client.post("/api/v1/exams/", self.payload, format="json").status_code, 403)
