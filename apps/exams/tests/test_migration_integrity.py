from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from uuid import uuid4

from apps.exams.models import Exam, ExamAttempt, ExamAssignment, ExamMonitoring, ExamParticipation, ExamAnalyticsSync, ExamManagementEvent, ExamProviderOperation


class MigrationIntegrityTests(TransactionTestCase):
    def test_incremental_sprints_preserve_observations_results_and_audit(self):
        final = [("exams", "0010_examattempt_participation_lease_until_and_more")]
        def migrate(target):
            executor = MigrationExecutor(connection)
            executor.migrate([("exams", target)])
            return executor.loader.project_state([("exams", target)]).apps
        try:
            old = migrate("0007_exammonitoring_examparticipation")
            now = timezone.now()
            user = old.get_model("accounts", "User").objects.create(username="migration-student", role="STUDENT")
            student = old.get_model("accounts", "StudentProfile").objects.create(user_id=user.pk)
            exam = old.get_model("exams", "Exam").objects.create(student_id=student.pk, title="Legacy published", status="PUBLISHED", manage_url="https://www.quiz24.ir/manage", provider_exam_id="91", provider_exam_code="321")
            assignment = old.get_model("exams", "ExamAssignment").objects.create(exam_id=exam.pk, student_id=student.pk)
            attempt = old.get_model("exams", "ExamAttempt").objects.create(exam_id=exam.pk, student_id=student.pk, status="COMPLETED", provider_username="legacy", score=80)
            old.get_model("exams", "ExamAnswer").objects.create(attempt_id=attempt.pk, order=1, answer="2")
            old.get_model("exams", "ExamMonitoring").objects.create(exam_id=exam.pk, last_success_at=now)
            old.get_model("exams", "ExamParticipation").objects.create(assignment_id=assignment.pk, status="COMPLETED", observed_at=now, identity_student_id=901, identity_school_id="174037", identity_verified_at=now)
            state = migrate("0008_examattempt_provider_rank_in_class_and_more")
            state.get_model("exams", "ExamAnalyticsSync").objects.create(exam_id=exam.pk, last_success_at=now, cursor_assignment_id=assignment.pk)
            state = migrate("0009_exam_archived_at_exam_creation_mode_and_more")
            state.get_model("exams", "ExamManagementEvent").objects.create(exam_id=exam.pk, action="ARCHIVE", previous={"title": "Legacy published"}, current={"status": "PUBLISHED"})
            state.get_model("exams", "ExamProviderOperation").objects.create(exam_id=exam.pk, kind="EDIT", state="IDLE", token=uuid4(), response_url="")
        finally:
            MigrationExecutor(connection).migrate(final)
        preserved = Exam.objects.get(pk=exam.pk)
        self.assertEqual((preserved.title, preserved.status, preserved.provider_exam_id, preserved.provider_exam_code), ("Legacy published", "PUBLISHED", "91", "321"))
        self.assertEqual(preserved.creation_mode, "MANUAL"); self.assertTrue(preserved.ever_published)
        result = ExamAttempt.objects.get(pk=attempt.pk)
        self.assertEqual(result.score, 80); self.assertEqual(result.answers.get().answer, "2")
        self.assertIsNone(result.participation_token); self.assertIsNone(result.participation_lease_until)
        self.assertIsNone(result.result_verified_at)
        self.assertEqual(ExamAssignment.objects.filter(exam=preserved).count(), 1)
        self.assertEqual(ExamMonitoring.objects.get(exam=preserved).last_success_at, now)
        self.assertEqual(ExamParticipation.objects.get(assignment_id=assignment.pk).status, "COMPLETED")
        self.assertEqual(ExamAnalyticsSync.objects.get(exam=preserved).cursor_assignment_id, assignment.pk)
        self.assertEqual(ExamManagementEvent.objects.get(exam=preserved).action, "ARCHIVE")
        self.assertEqual(ExamProviderOperation.objects.get(exam=preserved).state, "IDLE")
