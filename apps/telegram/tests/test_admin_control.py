from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection, TelegramAdminActionLog

User = get_user_model()


@override_settings(TELEGRAM_BOT_SERVICE_TOKEN="svc-secret")
class AdminControlTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", role=User.Role.ADMIN)
        self.counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.other_counselor_user = User.objects.create_user("other_c", role=User.Role.COUNSELOR)
        self.other_counselor = CounselorProfile.objects.create(user=self.other_counselor_user)
        self.student_user = User.objects.create_user("student")
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor)
        self.other_user = User.objects.create_user("other_student")
        self.other_student = StudentProfile.objects.create(user=self.other_user, counselor=self.other_counselor)
        self.chat_id = -100111
        self.user_id = 777
        TelegramGroupConnection.objects.create(student=self.student, telegram_chat_id=self.chat_id, is_active=True, chat_title="G")
        TelegramStudentConnection.objects.create(student=self.student, telegram_user_id=self.user_id, is_active=True, is_enabled=True)

    def test_admin_can_view_status(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/v1/admin/students/{self.student.pk}/telegram/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("connection", resp.data)

    def test_counselor_owner_can_view(self):
        self.client.force_authenticate(self.counselor_user)
        resp = self.client.get(f"/api/v1/admin/students/{self.student.pk}/telegram/")
        self.assertEqual(resp.status_code, 200)

    def test_counselor_cannot_suspend(self):
        self.client.force_authenticate(self.counselor_user)
        resp = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/suspend/", {"reason": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_student_cannot_access(self):
        self.client.force_authenticate(self.student_user)
        resp = self.client.get(f"/api/v1/admin/students/{self.student.pk}/telegram/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_disable_blocks_business(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/disable/", {"reason": "abuse"})
        # Now internal plan should be blocked
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id}, **{"HTTP_AUTHORIZATION": "Bearer svc-secret"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["code"], "telegram_disabled")

    def test_suspend_and_resume(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/suspend/", {"reason": "spam"})
        resp = self.client.get("/api/internal/v1/telegram/plan/today/", {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id}, **{"HTTP_AUTHORIZATION": "Bearer svc-secret"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["code"], "telegram_suspended")
        self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/resume/")
        resp2 = self.client.get("/api/internal/v1/telegram/plan/today/", {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id}, **{"HTTP_AUTHORIZATION": "Bearer svc-secret"})
        self.assertNotEqual(resp2.status_code, 403)

    @patch("apps.telegram.views_admin.bot_post_sync")
    def test_successful_ban(self, mock_bot):
        mock_bot.return_value = {"status": "banned"}
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/ban/", {"reason": "spam"})
        self.assertEqual(resp.status_code, 200)
        conn = TelegramStudentConnection.objects.get(student=self.student, is_active=True)
        self.assertTrue(conn.is_banned)
        mock_bot.assert_called_once()

    @patch("apps.telegram.views_admin.bot_post_sync", side_effect=Exception("bot down"))
    def test_failed_ban_does_not_update(self, mock_bot):
        from apps.telegram.bot_control import BotControlError
        mock_bot.side_effect = BotControlError("fail")
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/ban/", {"reason": "spam"})
        self.assertEqual(resp.status_code, 502)
        conn = TelegramStudentConnection.objects.get(student=self.student, is_active=True)
        self.assertFalse(conn.is_banned)

    @patch("apps.telegram.views_admin.bot_post_sync")
    def test_unban(self, mock_bot):
        # First ban
        mock_bot.return_value = {"status": "banned"}
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/ban/", {"reason": "x"})
        mock_bot.return_value = {"status": "unbanned"}
        resp = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/unban/")
        self.assertEqual(resp.status_code, 200)
        conn = TelegramStudentConnection.objects.get(student=self.student, is_active=True)
        self.assertFalse(conn.is_banned)

    @patch("apps.telegram.views_admin.bot_post_sync")
    def test_group_lock_calls_bot(self, mock_bot):
        mock_bot.return_value = {"status": "locked"}
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/group/lock/", {"reason": "exam"})
        self.assertEqual(resp.status_code, 200)
        mock_bot.assert_called_once()

    @patch("apps.telegram.views_admin.bot_post_sync")
    def test_resend_calls_bot(self, mock_bot):
        mock_bot.return_value = {"status": "sent"}
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/resend-plan/")
        self.assertEqual(resp.status_code, 200)
        mock_bot.assert_called_once()

    def test_audit_log_records(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/disable/", {"reason": "test reason"})
        log = TelegramAdminActionLog.objects.filter(student=self.student, action="DISABLE").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.reason, "test reason")
        self.assertTrue(log.success)
        self.assertEqual(log.actor_id, self.admin.pk)

    @override_settings(TELEGRAM_BOT_CONTROL_BASE_URL="", TELEGRAM_BOT_CONTROL_TOKEN="")
    def test_bot_unavailable_does_not_break_status(self):
        # No BOT_CONTROL configured by default in test, so reachable false but status 200
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/v1/admin/students/{self.student.pk}/telegram/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["bot"]["reachable"])

    @patch("apps.telegram.bot_control.httpx.Client")
    @override_settings(TELEGRAM_BOT_CONTROL_BASE_URL="http://bot", TELEGRAM_BOT_CONTROL_TOKEN="test")
    def test_missing_unlock_snapshot_returns_conflict_and_failed_audit(self, mock_client):
        import httpx
        mock_client.return_value.__enter__.return_value.post.return_value = httpx.Response(
            409, json={"detail": "permission_snapshot_missing"})
        self.client.force_authenticate(self.admin)
        response = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/group/unlock/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "permission_snapshot_missing")
        self.assertNotIn("locked", response.data)
        self.assertIn("تنظیمات قبلی گروه", response.data["detail"])
        log = TelegramAdminActionLog.objects.get(student=self.student, action="GROUP_UNLOCK")
        self.assertFalse(log.success)
        self.assertEqual(log.details, {"error": "permission_snapshot_missing"})

    @patch("apps.telegram.views_admin.bot_post_sync")
    def test_failed_unlock_response_never_reports_success(self, mock_bot):
        from apps.telegram.bot_control import BotControlUnavailable
        self.client.force_authenticate(self.admin)
        for result in ({"error": "permission_snapshot_missing"}, {"error": "bot_not_configured"}, {}):
            mock_bot.return_value = result
            response = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/group/unlock/")
            self.assertGreaterEqual(response.status_code, 400)
            self.assertNotIn("locked", response.data)
        mock_bot.side_effect = BotControlUnavailable("offline")
        response = self.client.post(f"/api/v1/admin/students/{self.student.pk}/telegram/group/unlock/")
        self.assertEqual(response.status_code, 502)
        logs = TelegramAdminActionLog.objects.filter(student=self.student, action="GROUP_UNLOCK")
        self.assertEqual(logs.count(), 4)
        self.assertFalse(logs.filter(success=True).exists())

    def test_audit_is_read_only(self):
        self.client.force_authenticate(self.admin)
        url = f"/api/v1/admin/students/{self.student.pk}/telegram/audit/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {}).status_code, 405)
        self.assertFalse(TelegramAdminActionLog.objects.exists())
