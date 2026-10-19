from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APITestCase, APIClient
from apps.accounts.models import User, CounselorProfile, StudentProfile
from apps.exams.exceptions import Quiz24Error
from apps.exams.models import Exam, ExamAssignment, ExamAttempt, ExamAnswer, ExamManagementEvent, ExamProviderOperation
from apps.exams.services.quiz24 import Quiz24Service


@override_settings(QUIZ24_REMOTE_DELETE_ENABLED=False, QUIZ24_USER_ID="174037", QUIZ24_API_KEY="mock-only", FRONTEND_BASE_URL="http://localhost:3000")
class ManagementTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("manager", role="COUNSELOR")
        self.profile = CounselorProfile.objects.create(user=self.owner)
        self.student = StudentProfile.objects.create(user=User.objects.create_user("student", role="STUDENT"), counselor=self.profile)
        self.other = User.objects.create_user("other", role="COUNSELOR")
        self.other_profile = CounselorProfile.objects.create(user=self.other)
        self.exam = Exam.objects.create(counselor=self.profile, student=self.student, title="آزمون", status="READY", creation_mode="MANUAL", provider_exam_id="91", provider_exam_code="3193168", question_count=4)
        self.client.force_authenticate(self.owner)
        patch("httpx.Client.post", side_effect=AssertionError("live call forbidden")).start()
        self.details = patch.object(Quiz24Service, "get_exam", return_value={"status": 100, "result": {"id": 91, "examCode": 3193168, "questionCount": 4}}).start()
        self.participants = patch.object(Quiz24Service, "get_exam_participants", return_value={"status": 100, "result": []}).start()
        self.edit = patch.object(Quiz24Service, "edit_existing_exam", return_value={"status": 100, "result": "https://www.quiz24.ir/user/ManageExam?id=opaque"}).start()
        self.delete = patch.object(Quiz24Service, "delete_existing_exam", return_value={"status": 100}).start()
        self.create = patch.object(Quiz24Service, "create_exam", side_effect=AssertionError("replacement forbidden")).start()
        self.addCleanup(patch.stopall)

    def url(self, action):
        return f"/api/v1/exams/{self.exam.pk}/{action}/"

    def local(self, action, **extra):
        self.exam.refresh_from_db()
        return self.client.post(self.url("management"), {"action": action, "expected_updated_at": self.exam.updated_at.isoformat(), **extra}, format="json")

    def provider(self, action="edit-questions", **extra):
        self.exam.refresh_from_db()
        return self.client.post(self.url(action), {"request_key": str(uuid4()), "expected_updated_at": self.exam.updated_at.isoformat(), **extra}, format="json")

    def test_lifecycle_history_and_no_remote_deletion(self):
        for action in ("rename", "archive", "unarchive", "trash", "restore"):
            response = self.local(action, title="عنوان جدید")
            self.assertEqual(response.status_code, 200, response.data)
        self.exam.refresh_from_db()
        self.assertEqual((self.exam.status, self.exam.title), ("READY", "عنوان جدید"))
        self.assertEqual(ExamManagementEvent.objects.count(), 5)
        self.assertEqual(ExamManagementEvent.objects.first().actor, self.owner)
        self.assertEqual(self.client.patch(self.url("history"), {}, format="json").status_code, 405)
        self.delete.assert_not_called()

    def test_publish_unpublish_and_restore(self):
        self.assertEqual(self.client.post(self.url("publish")).status_code, 200)
        count = ExamManagementEvent.objects.count()
        self.assertEqual(self.client.post(self.url("publish")).status_code, 200)
        self.assertEqual(ExamManagementEvent.objects.count(), count)
        self.assertEqual(self.local("unpublish").status_code, 200)
        self.client.post(self.url("publish"))
        self.local("archive")
        self.assertEqual(self.client.post(self.url("publish")).status_code, 400)
        self.assertEqual(self.local("unarchive").data["exam"]["status"], "READY")
        self.exam.refresh_from_db()
        self.exam.status = "DRAFT"; self.exam.save()
        self.assertEqual(self.client.post(self.url("publish")).status_code, 409)

    def test_attempt_answers_and_results_preserved(self):
        attempt = ExamAttempt.objects.create(exam=self.exam, student=self.student, status="COMPLETED", score=80)
        ExamAnswer.objects.create(attempt=attempt, order=1, answer="2")
        self.exam.status = "PUBLISHED"; self.exam.save()
        self.assertEqual(self.local("unpublish").status_code, 400)
        for action in ("archive", "trash", "restore", "unarchive"):
            self.assertEqual(self.local(action).status_code, 200)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.status, "CLOSED")
        self.assertEqual(ExamAttempt.objects.get().score, 80)
        self.assertEqual(ExamAnswer.objects.count(), 1)
        self.delete.assert_not_called()

    def test_inactive_exams_never_issue_new_links(self):
        for action in ("archive", "trash"):
            self.client.force_authenticate(self.owner); self.local(action)
            self.exam.status = "PUBLISHED"; self.exam.save()
            self.client.force_authenticate(self.student.user)
            with patch.object(Quiz24Service, "get_participation_url") as provider:
                self.assertEqual(self.client.post(self.url("start")).status_code, 409)
                provider.assert_not_called()

    def test_ownership_student_and_practice_isolation(self):
        for user, expected in ((self.other, 404), (self.student.user, 403)):
            self.client.force_authenticate(user)
            for action in ("management", "history"):
                self.assertEqual(self.client.get(self.url(action)).status_code, expected)
            self.assertEqual(self.provider().status_code, expected)
        self.client.force_authenticate(self.owner)
        self.exam.is_personal_practice = True; self.exam.save()
        self.assertEqual(self.local("trash").status_code, 404)

    def test_stale_versions_and_illegal_transitions(self):
        old = self.exam.updated_at.isoformat()
        self.local("rename", title="جدید")
        response = self.client.post(self.url("management"), {"action": "archive", "expected_updated_at": old}, format="json")
        self.assertEqual(response.status_code, 400)
        self.exam.refresh_from_db(); self.assertIsNone(self.exam.archived_at)
        self.local("trash")
        self.assertEqual(self.local("rename", title="عنوان").status_code, 400)
        self.assertEqual(self.local("archive").status_code, 400)

    def test_bulk_atomic_ownership_stale_duplicates(self):
        other = Exam.objects.create(counselor=self.other_profile, title="Other")
        second = Exam.objects.create(counselor=self.profile, title="Second")
        def bulk(rows):
            return self.client.post("/api/v1/exams/management/bulk/", {"action": "archive", "exams": [{"id": exam.pk, "expected_updated_at": exam.updated_at.isoformat()} for exam in rows]}, format="json")
        self.assertEqual(bulk([self.exam, other]).status_code, 400)
        self.assertEqual(bulk([self.exam, self.exam]).status_code, 400)
        stale = Exam.objects.get(pk=second.pk); second.save()
        self.assertEqual(bulk([self.exam, stale]).status_code, 400)
        self.exam.refresh_from_db(); self.assertIsNone(self.exam.archived_at)
        second.refresh_from_db()
        self.assertEqual(bulk([self.exam, second]).status_code, 200)
        self.assertEqual(ExamManagementEvent.objects.count(), 2)

    def test_same_provider_edit_fenced_idempotent_reconciled(self):
        key = str(uuid4())
        body = {"request_key": key, "expected_updated_at": self.exam.updated_at.isoformat()}
        first = self.client.post(self.url("edit-questions"), body, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(self.client.post(self.url("edit-questions"), body, format="json").status_code, 200)
        self.edit.assert_called_once()
        self.assertEqual(self.edit.call_args.args[0], "91")
        self.assertIn(f"edit_session={key}", self.edit.call_args.args[1])
        self.assertEqual(self.provider().status_code, 409)
        self.assertEqual(self.local("trash").status_code, 400)
        self.assertEqual(self.client.post(self.url("sync"), {"edit_session": str(uuid4())}, format="json").status_code, 409)
        self.assertEqual(self.client.post(self.url("sync"), {"edit_session": key}, format="json").status_code, 200)
        self.exam.refresh_from_db(); self.assertEqual(self.exam.provider_exam_id, "91")
        self.assertEqual(ExamProviderOperation.objects.get().state, "IDLE")
        self.assertEqual(self.client.post(self.url("edit-questions"), body, format="json").status_code, 409)
        self.create.assert_not_called()

    def test_edit_blocks_automatic_activity_and_identity_mismatch(self):
        self.exam.creation_mode = "AUTOMATIC"; self.exam.save()
        self.assertEqual(self.provider().status_code, 409)
        self.exam.creation_mode = "MANUAL"; self.exam.save()
        self.details.return_value = {"status": 100, "result": {"id": 999, "questionCount": 4}}
        self.assertEqual(self.provider().status_code, 502)
        self.edit.assert_not_called()
        ExamAttempt.objects.create(exam=self.exam, student=self.student)
        self.assertEqual(self.provider().status_code, 409)

    def test_provider_participation_blocks_edit(self):
        self.participants.return_value = {"status": 100, "result": [{"userName": "09123456789", "isStarted": True, "isComplete": False}]}
        self.assertEqual(self.provider().status_code, 502)
        self.edit.assert_not_called()

    def test_delete_flag_confirmation_identity_and_success(self):
        self.local("trash")
        confirmation = dict(confirm_remote_delete=True, confirmed_title=self.exam.title, provider_exam_id="91")
        self.assertEqual(self.provider("remote-delete", **confirmation).status_code, 403)
        with override_settings(QUIZ24_REMOTE_DELETE_ENABLED=True):
            self.assertEqual(self.provider("remote-delete").status_code, 400)
            self.assertEqual(self.provider("remote-delete", **{**confirmation, "provider_exam_id": "92"}).status_code, 400)
            response = self.provider("remote-delete", **confirmation)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["state"], "DELETED")
            self.assertEqual(self.local("restore").status_code, 400)
        self.delete.assert_called_once_with("91")
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())

    @override_settings(QUIZ24_REMOTE_DELETE_ENABLED=True)
    def test_delete_ambiguous_never_retries_or_reports_success(self):
        self.local("trash")
        self.delete.side_effect = Quiz24Error(None, "timeout")
        confirmation = dict(confirm_remote_delete=True, confirmed_title=self.exam.title, provider_exam_id="91")
        response = self.provider("remote-delete", **confirmation)
        self.assertEqual((response.status_code, response.data["state"]), (502, "UNKNOWN"))
        self.assertEqual(self.provider("remote-delete", **confirmation).status_code, 409)
        self.delete.assert_called_once()
        self.assertEqual(ExamManagementEvent.objects.first().outcome, "UNKNOWN")

    @override_settings(QUIZ24_REMOTE_DELETE_ENABLED=True)
    def test_delete_blocks_assignments_attempts_and_ever_published(self):
        self.local("trash")
        confirmation = dict(confirm_remote_delete=True, confirmed_title=self.exam.title, provider_exam_id="91")
        assignment = ExamAssignment.objects.create(exam=self.exam, student=self.student, assigned_by=self.owner)
        self.assertEqual(self.provider("remote-delete", **confirmation).status_code, 409)
        assignment.delete()
        self.exam.ever_published = True; self.exam.save()
        self.assertEqual(self.provider("remote-delete", **confirmation).status_code, 409)
        self.exam.ever_published = False; self.exam.save()
        ExamAttempt.objects.create(exam=self.exam, student=self.student)
        self.assertEqual(self.provider("remote-delete", **confirmation).status_code, 409)
        self.delete.assert_not_called()

    def test_reset_unsupported(self):
        self.assertFalse(self.client.get(self.url("management")).data["reset_supported"])
        self.assertEqual(self.client.post(self.url("reset")).status_code, 404)

    def test_published_history_blocks_edit_even_after_unpublishing(self):
        self.client.post(self.url("publish"))
        self.assertEqual(self.local("unpublish").status_code, 200)
        self.assertEqual(self.provider().status_code, 409)
        self.assertFalse(self.client.get(self.url("management")).data["can_edit_questions"])
        self.edit.assert_not_called()

    def test_publish_rejects_stale_version(self):
        old = self.exam.updated_at.isoformat()
        self.local("rename", title="Changed")
        response = self.client.post(self.url("publish"), {"expected_updated_at": old}, format="json")
        self.assertEqual(response.status_code, 409)
        self.exam.refresh_from_db(); self.assertEqual(self.exam.status, "READY")

    def test_management_keeps_historical_result_visibility(self):
        ExamAttempt.objects.create(exam=self.exam, student=self.student, status="COMPLETED")
        response = self.local("trash")
        self.assertTrue(response.data["exam"]["result_available"])
        self.assertTrue(self.client.get(self.url("management")).data["exam"]["result_available"])

    @override_settings(QUIZ24_REMOTE_DELETE_ENABLED=True)
    def test_definite_remote_rejection_and_malformed_response_are_not_success(self):
        self.local("trash")
        confirmation = dict(confirm_remote_delete=True, confirmed_title=self.exam.title, provider_exam_id="91")
        self.delete.side_effect = Quiz24Error(115, "wrong owner")
        response = self.provider("remote-delete", **confirmation)
        self.assertEqual((response.status_code, response.data["state"]), (502, "FAILED"))
        self.delete.side_effect = None; self.delete.return_value = {"unexpected": "response"}
        response = self.provider("remote-delete", **confirmation)
        self.assertEqual((response.status_code, response.data["state"]), (502, "UNKNOWN"))

    def test_provider_http_outside_claim_transaction(self):
        depth = len(connection.atomic_blocks)
        def provider(*args):
            self.assertEqual(len(connection.atomic_blocks), depth)
            self.assertEqual(ExamProviderOperation.objects.get().state, "PENDING")
            return {"status": 100, "result": "https://www.quiz24.ir/edit"}
        self.edit.side_effect = provider
        self.assertEqual(self.provider().status_code, 200)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking; SQLite cannot verify concurrency")
class ManagementConcurrencyTests(TransactionTestCase):
    def test_same_version_only_mutates_once(self):
        user = User.objects.create_user("concurrent-manager", role="COUNSELOR")
        profile = CounselorProfile.objects.create(user=user)
        exam = Exam.objects.create(counselor=profile, title="Original")
        barrier = Barrier(2)
        def change(title):
            close_old_connections()
            client = APIClient(); client.force_authenticate(user)
            barrier.wait()
            try:
                return client.post(f"/api/v1/exams/{exam.pk}/management/", {"action": "rename", "title": title, "expected_updated_at": exam.updated_at.isoformat()}, format="json").status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ("First", "Second")))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(ExamManagementEvent.objects.count(), 1)

    @override_settings(QUIZ24_USER_ID="174037", QUIZ24_API_KEY="mock-only", FRONTEND_BASE_URL="http://localhost:3000")
    def test_concurrent_provider_claim_only_calls_editor_once(self):
        user = User.objects.create_user("provider-race", role="COUNSELOR")
        profile = CounselorProfile.objects.create(user=user)
        exam = Exam.objects.create(counselor=profile, title="Original", status="READY", creation_mode="MANUAL", provider_exam_id="91", provider_exam_code="3193168", question_count=4)
        barrier = Barrier(2)
        def edit(_):
            close_old_connections()
            client = APIClient(); client.force_authenticate(user)
            barrier.wait()
            try:
                return client.post(f"/api/v1/exams/{exam.pk}/edit-questions/", {"request_key": str(uuid4()), "expected_updated_at": exam.updated_at.isoformat()}, format="json").status_code
            finally:
                close_old_connections()
        with patch.object(Quiz24Service, "get_exam", return_value={"status": 100, "result": {"id": 91, "questionCount": 4}}), patch.object(Quiz24Service, "get_exam_participants", return_value={"status": 100, "result": []}), patch.object(Quiz24Service, "edit_existing_exam", return_value={"status": 100, "result": "https://www.quiz24.ir/edit"}) as provider:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(edit, (1, 2)))
        self.assertEqual(sorted(results), [200, 409])
        provider.assert_called_once()
