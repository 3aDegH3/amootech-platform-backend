from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TransactionTestCase
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.exams.models import Exam, ExamAssignment, ExamAttempt


class AssignmentTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.owner)
        self.other_owner = User.objects.create_user(username="other-owner", role=User.Role.COUNSELOR)
        self.other_counselor = CounselorProfile.objects.create(user=self.other_owner)
        self.students = [StudentProfile.objects.create(user=User.objects.create_user(username=f"s{i}", role=User.Role.STUDENT), counselor=self.counselor) for i in range(3)]
        self.exam = Exam.objects.create(counselor=self.counselor, student=self.students[0], title="shared", status=Exam.Status.PUBLISHED, provider_exam_code="same-code")
        self.url = f"/api/v1/exams/{self.exam.pk}/"
        self.client.force_authenticate(self.owner)

    def assign(self, ids):
        return self.client.post(self.url + "assignments/", {"student_ids": ids}, format="json")

    def test_bulk_idempotent_local_assignment_and_constraint(self):
        with patch("apps.exams.views.Quiz24Service.create_exam") as manual, patch("apps.exams.views.Quiz24Service.create_online_exam") as automatic:
            for _ in range(2):
                self.assertEqual(self.assign([s.pk for s in self.students]).status_code, 200)
            manual.assert_not_called()
            automatic.assert_not_called()
        self.assertEqual(Exam.objects.count(), 1)
        self.assertEqual(self.exam.assignments.count(), 3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExamAssignment.objects.create(exam=self.exam, student=self.students[0])

    def test_bulk_invalid_rolls_back(self):
        self.students[2].counselor = self.other_counselor
        self.students[2].save()
        for invalid in (self.students[2].pk, 999999):
            self.assertEqual(self.assign([self.students[1].pk, invalid]).status_code, 400)
            self.assertFalse(self.exam.assignments.exists())

    def test_ownership_and_practice(self):
        self.client.force_authenticate(self.other_owner)
        self.assertEqual(self.assign([self.students[1].pk]).status_code, 404)
        self.client.force_authenticate(self.owner)
        self.exam.is_personal_practice = True
        self.exam.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.assign([self.students[1].pk]).status_code, 404)

    def test_group_visibility_start_and_own_result(self):
        self.assign([s.pk for s in self.students[:2]])
        ExamAttempt.objects.create(exam=self.exam, student=self.students[0], provider_username="first", status="COMPLETED", score=90)
        self.client.force_authenticate(self.students[1].user)
        rows = self.client.get("/api/v1/exams/").data["results"]
        self.assertEqual([row["id"] for row in rows], [self.exam.pk])
        self.assertFalse(rows[0]["result_available"])
        self.assertEqual(self.client.get(self.url + "result/").status_code, 404)
        with patch("apps.exams.views.student_quiz24_mobile", return_value="second") as mobile, patch("apps.exams.views.Quiz24Service.get_participation_url", return_value="https://example.test") as provider:
            for _ in range(2):
                self.assertEqual(self.client.post(self.url + "start/").status_code, 200)
            mobile.assert_called_with(self.students[1])
            provider.assert_called_with(user_name="second", exam_code="same-code")
        self.assertEqual(self.exam.attempts.count(), 2)
        self.assertEqual(self.client.get(self.url + "result/").data["student"], self.students[1].pk)
        self.assertEqual(self.assign([self.students[2].pk]).status_code, 403)
        self.client.force_authenticate(self.students[2].user)
        self.assertEqual(self.client.post(self.url + "start/").status_code, 404)

    def test_group_result_sync_uses_authenticated_student_and_preserves_other_answers(self):
        self.assign([s.pk for s in self.students[:2]])
        first = ExamAttempt.objects.create(exam=self.exam, student=self.students[0], provider_username="first", score=99)
        first.answers.create(order=1, answer="4")
        self.client.force_authenticate(self.students[1].user)
        with patch("apps.exams.views.student_quiz24_mobile", return_value="second"), patch("apps.exams.views.Quiz24Service.get_exam_result", return_value={"status": 100, "result": {"score": 75, "questions": [{"Order": 1, "Answer": "2", "CorrectAnswer": "2"}]}}) as provider, patch("apps.exams.views.Quiz24Service.get_answersheet", return_value={}):
            for _ in range(2):
                response = self.client.post(self.url + "result/", {"student": self.students[0].pk}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["student"], self.students[1].pk)
            provider.assert_called_with("second", "same-code")
        self.assertEqual(self.exam.attempts.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.score, 99)
        self.assertEqual(first.answers.get().answer, "4")

    def test_removal_blocks_started_and_revokes_legacy_access(self):
        self.assign([s.pk for s in self.students[:2]])
        ExamAttempt.objects.create(exam=self.exam, student=self.students[1], provider_username="second")
        self.assertEqual(self.client.delete(self.url + "assignments/", {"student_ids": [s.pk for s in self.students[:2]]}, format="json").status_code, 409)
        self.assertEqual(self.exam.assignments.count(), 2)
        self.assertEqual(self.client.delete(self.url + "assignments/", {"student_ids": [self.students[0].pk]}, format="json").status_code, 200)
        self.client.force_authenticate(self.students[0].user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_reassignment_hides_historical_metadata(self):
        self.assign([self.students[0].pk])
        self.students[0].counselor = self.other_counselor
        self.students[0].save()
        self.assertIsNone(self.client.get(self.url).data["student"])
        self.assertEqual(self.client.get(self.url + "assignments/").data["results"], [])
        self.assertEqual(self.client.get(self.url + "result/").status_code, 404)
        self.client.force_authenticate(self.other_owner)
        self.assertEqual(self.client.get(self.url).status_code, 404)


class BackfillTests(TransactionTestCase):
    def test_existing_exam_preserved_and_backfill_idempotent(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        before = [("accounts", "0004_student_mobile_verification"), ("exams", "0004_examcreation")]
        after = [("accounts", "0004_student_mobile_verification"), ("exams", "0010_examattempt_participation_lease_until_and_more")]
        executor.migrate(before)
        try:
            old = executor.loader.project_state(before).apps
            user = old.get_model("accounts", "User").objects.create(username="legacy", role="STUDENT")
            student = old.get_model("accounts", "StudentProfile").objects.create(user_id=user.pk)
            exam = old.get_model("exams", "Exam").objects.create(student_id=student.pk, title="legacy", provider_exam_code="legacy-code")
            attempt = old.get_model("exams", "ExamAttempt").objects.create(exam_id=exam.pk, student_id=student.pk, provider_username="legacy")
            old.get_model("exams", "ExamAnswer").objects.create(attempt_id=attempt.pk, order=1, answer="2")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(after)
        self.assertEqual(ExamAssignment.objects.get(exam_id=exam.pk).student_id, student.pk)
        preserved = Exam.objects.get(pk=exam.pk)
        self.assertEqual(preserved.provider_exam_code, "legacy-code")
        self.assertIsNone(preserved.scheduled_start_at)
        self.assertIsNone(preserved.participation_deadline_at)
        self.assertIsNone(preserved.result_release_at)
        self.assertEqual(ExamAttempt.objects.get(pk=attempt.pk).answers.get().answer, "2")
