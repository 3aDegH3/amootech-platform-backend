import io
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITransactionTestCase

from apps.accounts.mobile import normalize_mobile
from apps.accounts.models import CounselorProfile, MobileChallenge, MobileOTPQuota, Quiz24StudentRegistration, StudentProfile, User
from apps.accounts.services.otp import DevelopmentOTPAdapter, DeliveryUnavailable
from apps.accounts.services.quiz24_sync import sync_student
from apps.exams.exceptions import Quiz24Error
from apps.exams.models import Exam
from apps.exams.services.quiz24 import student_quiz24_mobile


class RecordingOTPAdapter(DevelopmentOTPAdapter):
    sent = []

    def send(self, mobile, code, *, student_id, nonce):
        if not settings.DEBUG or not settings.STUDENT_OTP_DEV_ENABLED:
            raise DeliveryUnavailable()
        assert not connection.in_atomic_block, "SMS delivery must not hold a transaction"
        self.sent.append((mobile, code))


@override_settings(DEBUG=True, STUDENT_OTP_DEV_ENABLED=True, STUDENT_OTP_BACKEND="apps.accounts.tests.test_mobile.RecordingOTPAdapter", QUIZ24_USER_ID="174037", QUIZ24_API_KEY="mock-only")
class MobileVerificationTests(APITransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="student-mobile", role="STUDENT", first_name="نام", last_name="خانوادگی")
        self.student = StudentProfile.objects.create(user=self.user)
        self.client.force_authenticate(self.user)
        RecordingOTPAdapter.sent.clear()
        self.register = patch("apps.accounts.services.quiz24_sync.Quiz24Service.register_student", return_value={"status": 100, "userId": 901}).start()
        self.addCleanup(patch.stopall)

    def request(self, mobile="+989123456789"):
        return self.client.post("/api/v1/auth/mobile/request/", {"mobile": mobile}, format="json")

    def verify(self, code=None):
        return self.client.post("/api/v1/auth/mobile/verify/", {"code": code or RecordingOTPAdapter.sent[-1][1]}, format="json")

    def ready(self):
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.verify().status_code, 200)
        self.student.refresh_from_db()

    def test_normalization_unique_mobile_and_never_verified_by_format(self):
        self.assertEqual(normalize_mobile("+989123456789"), "09123456789")
        self.assertEqual(normalize_mobile("۰۹۱۲۳۴۵۶۷۸۹"), "09123456789")
        self.student.mobile = "+989123456789"
        self.student.save()
        self.assertIsNone(self.student.mobile_verified_at)
        other = StudentProfile.objects.create(user=User.objects.create_user("other", role="STUDENT"))
        self.client.force_authenticate(other.user)
        response = self.client.patch("/api/v1/auth/profile/student/", {"mobile": "09123456789"}, format="json")
        self.assertEqual(response.status_code, 400)
        for invalid in ("0912", "989123456789", "00989123456789"):
            with self.assertRaises(Exception):
                normalize_mobile(invalid)

    def test_hashed_code_no_api_disclosure_and_automatic_sync_outside_transaction(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        code = RecordingOTPAdapter.sent[-1][1]
        self.assertNotIn(code, str(response.data))
        self.assertNotEqual(MobileChallenge.objects.get().code_hash, code)
        def register(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(kwargs["mobile"], "09123456789")
            self.assertGreaterEqual(len(kwargs["password"]), 32)
            return {"status": 100, "userId": 901}
        self.register.side_effect = register
        response = self.verify(code)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("password", str(response.data))
        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.mobile_verified_at)
        self.assertEqual((self.student.quiz24_student_id, self.student.quiz24_sync_status), (901, "SYNCED"))
        self.assertEqual(self.verify(code).status_code, 400)
        self.register.assert_called_once()

    def test_expiration_attempt_limits_and_no_attempt_rollback(self):
        self.request()
        code = RecordingOTPAdapter.sent[-1][1]
        wrong = "000001" if code != "000001" else "000002"
        for _ in range(5):
            self.assertEqual(self.verify(wrong).status_code, 400)
        self.assertEqual(MobileChallenge.objects.get().attempts, 5)
        self.assertEqual(self.verify(code).status_code, 400)
        MobileChallenge.objects.update(attempts=0, expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.verify(code).status_code, 400)
        self.register.assert_not_called()

    def test_resend_cooldown_hourly_quota_and_old_code_invalidation(self):
        now = timezone.now()
        with patch("apps.accounts.services.otp.timezone.now", return_value=now):
            self.assertEqual(self.request().status_code, 200)
            old_nonce = MobileChallenge.objects.get().nonce
            self.assertEqual(self.request().status_code, 429)
        for i in range(1, 5):
            with patch("apps.accounts.services.otp.timezone.now", return_value=now + timedelta(seconds=61*i)):
                self.assertEqual(self.request().status_code, 200)
        self.assertNotEqual(MobileChallenge.objects.get().nonce, old_nonce)
        with patch("apps.accounts.services.otp.timezone.now", return_value=now + timedelta(minutes=10)):
            self.assertEqual(self.request().status_code, 429)
        self.register.assert_not_called()

    def test_production_cannot_use_development_adapter_or_unconfigured_sender(self):
        for backend in ("", "apps.accounts.services.otp.DevelopmentOTPAdapter"):
            with override_settings(DEBUG=False, STUDENT_OTP_DEV_ENABLED=True, STUDENT_OTP_BACKEND=backend):
                self.assertEqual(self.request().status_code, 503)
                with self.assertRaises(CommandError):
                    call_command("dev_mobile_otp", student=self.user.username, stdout=io.StringIO())
        self.assertFalse(MobileChallenge.objects.exists())
        with override_settings(STUDENT_OTP_DEV_ENABLED=False, STUDENT_OTP_BACKEND=""):
            self.assertEqual(self.request().status_code, 503)

    def test_delivery_failure_invalidates_challenge(self):
        with patch.object(RecordingOTPAdapter, "send", side_effect=RuntimeError("secret")):
            response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", str(response.data))
        self.assertTrue(MobileChallenge.objects.get().consumed)

    def test_duplicate_requests_do_not_register_again_and_school_identity_is_checked(self):
        self.ready()
        self.assertEqual(self.client.post("/api/v1/auth/quiz24/sync/").status_code, 200)
        self.register.assert_called_once()
        self.assertEqual(student_quiz24_mobile(self.student), "09123456789")
        with override_settings(QUIZ24_USER_ID="999"):
            with self.assertRaises(Quiz24Error):
                student_quiz24_mobile(self.student)

    def test_existing_account_is_not_adopted_or_overwritten(self):
        self.register.side_effect = Quiz24Error(104, "secret password")
        self.ready()
        self.student.refresh_from_db()
        self.assertEqual(self.student.quiz24_sync_status, "CONFLICT")
        self.assertIsNone(self.student.quiz24_student_id)
        self.assertNotIn("secret", self.student.quiz24_sync_error)
        self.client.post("/api/v1/auth/quiz24/sync/")
        self.register.assert_called_once()
        self.assertIsNotNone(self.student.mobile_verified_at)

    def test_definite_failure_can_retry_after_cooldown(self):
        self.register.side_effect = Quiz24Error(109, "bad school")
        self.ready()
        self.assertEqual(self.student.quiz24_sync_status, "FAILED")
        self.client.post("/api/v1/auth/quiz24/sync/")
        self.register.assert_called_once()
        Quiz24StudentRegistration.objects.update(attempted_at=timezone.now() - timedelta(seconds=61))
        self.register.side_effect = None
        self.assertEqual(self.client.post("/api/v1/auth/quiz24/sync/").status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.quiz24_sync_status, "SYNCED")
        self.assertEqual(self.register.call_count, 2)

    def test_uncertain_network_result_or_malformed_success_never_retries_registration(self):
        self.register.side_effect = Quiz24Error(None, "timeout")
        self.ready()
        self.assertEqual(self.student.quiz24_sync_status, "UNKNOWN")
        Quiz24StudentRegistration.objects.update(attempted_at=timezone.now() - timedelta(days=1))
        self.client.post("/api/v1/auth/quiz24/sync/")
        self.register.assert_called_once()

    def test_changed_number_invalidates_verification_and_sync_and_old_challenge(self):
        self.ready()
        self.assertEqual(self.client.patch("/api/v1/auth/profile/student/", {"mobile": "+989123456788"}, format="json").status_code, 200)
        self.student.refresh_from_db()
        self.assertIsNone(self.student.mobile_verified_at)
        self.assertIsNone(self.student.quiz24_student_id)
        self.assertEqual(self.student.quiz24_sync_status, "NOT_READY")
        self.assertFalse(MobileChallenge.objects.exists())
        with self.assertRaises(Quiz24Error):
            student_quiz24_mobile(self.student)
        MobileOTPQuota.objects.all().delete()
        self.request("09123456788")
        self.verify()
        self.student.refresh_from_db()
        self.assertEqual(self.student.quiz24_sync_status, "SYNCED")
        self.assertEqual(self.register.call_count, 2)

    def test_change_while_provider_in_flight_does_not_attach_wrong_identity(self):
        def change(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            student = StudentProfile.objects.get(pk=self.student.pk)
            student.mobile = "09123456788"
            student.save()
            return {"status": 100, "userId": 901}
        self.register.side_effect = change
        self.ready()
        self.assertEqual(self.student.mobile, "09123456788")
        self.assertIsNone(self.student.mobile_verified_at)
        self.assertIsNone(self.student.quiz24_student_id)

    def test_inflight_registration_claim_prevents_parallel_registration(self):
        def reenter(**kwargs):
            sync_student(self.student.pk)
            return {"status": 100, "userId": 901}
        self.register.side_effect = reenter
        self.ready()
        self.register.assert_called_once()

    def test_permissions_and_staff_cannot_set_verification(self):
        counselor = CounselorProfile.objects.create(user=User.objects.create_user("counselor", role="COUNSELOR"))
        self.student.counselor = counselor
        self.student.save()
        for user in (self.user, counselor.user):
            self.client.force_authenticate(user)
            endpoint = "/api/v1/auth/profile/student/" if user == self.user else f"/api/v1/students/{self.student.pk}/mobile/"
            self.assertEqual(self.client.patch(endpoint, {"mobile_verified_at": timezone.now().isoformat()}, format="json").status_code, 400)
        self.assertEqual(self.request().status_code, 403)
        self.assertEqual(self.client.patch(f"/api/v1/students/{self.student.pk}/mobile/", {"mobile": "09123456789"}, format="json").status_code, 200)
        other = StudentProfile.objects.create(user=User.objects.create_user("other", role="STUDENT"))
        self.assertEqual(self.client.patch(f"/api/v1/students/{other.pk}/mobile/", {"mobile": "09123456788"}, format="json").status_code, 404)
        self.client.force_authenticate(user=None)
        self.assertIn(self.request().status_code, (401, 403))

    def test_counselor_creation_is_owned_and_unverified(self):
        counselor = CounselorProfile.objects.create(user=User.objects.create_user("creator", role="COUNSELOR"))
        self.client.force_authenticate(counselor.user)
        payload = dict(username="created", password="password", first_name="نام", last_name="خانوادگی", mobile="+989123456789")
        response = self.client.post("/api/v1/students/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        student = StudentProfile.objects.get(user__username="created")
        self.assertEqual(student.counselor, counselor)
        self.assertIsNone(student.mobile_verified_at)
        self.assertEqual(self.client.post("/api/v1/students/", {**payload, "username": "bad", "counselor": counselor.pk}, format="json").status_code, 400)
        self.register.assert_not_called()

    def test_exam_start_uses_only_verified_mobile_and_requires_completed_sync(self):
        self.ready()
        exam = Exam.objects.create(student=self.student, title="exam", status="PUBLISHED", provider_exam_code="123")
        with patch("apps.exams.views.Quiz24Service.get_participation_url", return_value="https://example.test/exam") as start:
            self.assertEqual(self.client.post(f"/api/v1/exams/{exam.pk}/start/", {"userName": "09120000000"}).status_code, 200)
            start.assert_called_once_with(user_name="09123456789", exam_code="123")
            StudentProfile.objects.filter(pk=self.student.pk).update(quiz24_sync_status="FAILED")
            self.assertEqual(self.client.post(f"/api/v1/exams/{exam.pk}/start/").status_code, 409)
            self.assertEqual(start.call_count, 1)

    def test_seed_demo_never_delivers_otp_or_registers_students(self):
        with patch.dict("os.environ", {"ALLOW_DEMO_SEED": "true"}), patch.object(RecordingOTPAdapter, "send") as sms:
            call_command("seed_demo", counselors=1, students_per_counselor=1, history_days=1, stdout=io.StringIO())
            call_command("seed_demo", counselors=1, students_per_counselor=1, history_days=1, reset=True, stdout=io.StringIO())
            sms.assert_not_called()
        self.register.assert_not_called()
        demo = StudentProfile.objects.get(user__username="demo_student")
        self.assertIsNone(demo.mobile)
        self.assertIsNone(demo.mobile_verified_at)

    def test_development_mailbox_is_private_and_console_read_is_explicit_and_one_time(self):
        import json
        import stat
        from pathlib import Path
        from tempfile import TemporaryDirectory
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with TemporaryDirectory() as directory, override_settings(STUDENT_OTP_BACKEND="", STUDENT_OTP_DEV_OUTBOX=directory):
            self.assertEqual(self.request().status_code, 200)
            path = Path(directory) / f"{self.student.pk}.json"
            code = json.loads(path.read_text())["code"]
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
            with self.assertRaises(CommandError):
                call_command("dev_mobile_otp", student=self.user.username, stdout=io.StringIO())
            output = Terminal()
            call_command("dev_mobile_otp", student=self.user.username, stdout=output)
            self.assertEqual(output.getvalue().strip(), code)
            self.assertFalse(path.exists())
        self.register.assert_not_called()

    def test_malformed_provider_success_preserves_verification_and_blocks_duplicate_registration(self):
        self.register.return_value = {"status": 100, "result": {"unexpected": 901}}
        self.ready()
        self.assertEqual(self.student.quiz24_sync_status, "UNKNOWN")
        self.assertIsNotNone(self.student.mobile_verified_at)
        self.client.post("/api/v1/auth/quiz24/sync/")
        self.register.assert_called_once()

    def test_persian_otp_digits_are_normalized(self):
        self.request()
        code = RecordingOTPAdapter.sent[-1][1].translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
        self.assertEqual(self.verify(code).status_code, 200)
