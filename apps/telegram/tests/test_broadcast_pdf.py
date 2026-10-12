from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.daily_reports.models import DailyReport
from apps.planning.models import Plan
from apps.telegram.automation import publish_pdf_after_commit
from apps.telegram.bot_control import BotControlUnavailable
from apps.telegram.models import TelegramGroupConnection, TelegramPlanDelivery
from . import test_report_api


@override_settings(TELEGRAM_BOT_SERVICE_TOKEN="svc-secret")
class BroadcastAndPublicationTests(APITestCase):
    setUp = test_report_api.TelegramReportTests.setUp
    url = "/api/v1/counselor/telegram/broadcast/"

    def other_student(self):
        user = User.objects.create_user("other", role=User.Role.COUNSELOR)
        counselor = CounselorProfile.objects.create(user=user)
        student = StudentProfile.objects.create(user=User.objects.create_user("other_student"), counselor=counselor)
        TelegramGroupConnection.objects.create(student=student, telegram_chat_id=-100222)
        return user

    def test_role_scope_and_student_denial(self):
        self.other_student()
        self.client.force_authenticate(self.student_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"message": "hello"}).status_code, 403)
        self.client.force_authenticate(self.counselor_user)
        self.assertEqual(self.client.get(self.url).data["total"], 1)
        with patch("apps.telegram.bot_control.bot_broadcast_sync", return_value={"sent": 1, "failed": 0, "results": []}) as send:
            self.assertEqual(self.client.post(self.url, {"message": "hello"}).status_code, 200)
            send.assert_called_once_with([self.chat_id], "hello")
        self.client.force_authenticate(User.objects.create_user("admin", role=User.Role.ADMIN))
        self.assertEqual(self.client.get(self.url).data["total"], 2)
        with patch("apps.telegram.bot_control.bot_broadcast_sync", return_value={"sent": 2, "failed": 0}) as send:
            self.assertEqual(self.client.post(self.url, {"message": "hello"}).data["total"], 2)
            self.assertEqual(set(send.call_args.args[0]), {self.chat_id, -100222})

    def test_all_groups_beyond_200_and_partial_failure(self):
        users = User.objects.bulk_create([User(username=f"bulk{i}") for i in range(201)])
        students = StudentProfile.objects.bulk_create([StudentProfile(user=user, counselor=self.counselor) for user in users])
        TelegramGroupConnection.objects.bulk_create([TelegramGroupConnection(student=student, telegram_chat_id=-200000-i) for i, student in enumerate(students)])
        self.client.force_authenticate(self.counselor_user)
        with patch("apps.telegram.bot_control.bot_broadcast_sync", side_effect=[{"sent": 200, "failed": 0}, BotControlUnavailable()]) as send:
            response = self.client.post(self.url, {"message": "hello"})
        self.assertEqual(response.data["total"], 202)
        self.assertEqual(response.data["sent"], 200)
        self.assertEqual(response.data["failed"], 2)
        self.assertEqual([len(call.args[0]) for call in send.call_args_list], [200, 2])

    def test_message_validation_and_inactive_group(self):
        self.client.force_authenticate(self.counselor_user)
        for message in [None, 42, "  ", "x" * 4001]:
            self.assertEqual(self.client.post(self.url, {"message": message}, format="json").status_code, 400)
        TelegramGroupConnection.objects.update(is_active=False)
        self.assertEqual(self.client.get(self.url).data["total"], 0)
        self.assertEqual(self.client.post(self.url, {"message": "hello"}).status_code, 404)

    def test_publish_queues_exact_plan_after_commit(self):
        self.plan.status, self.plan.published_at = Plan.Status.DRAFT, None
        self.plan.save()
        self.client.force_authenticate(self.counselor_user)
        with patch("apps.telegram.automation.bot_post_sync", return_value={"status": "queued"}) as post:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/api/v1/planning/plans/{self.plan.pk}/publish/")
        self.assertEqual(response.status_code, 200, response.data)
        delivery = TelegramPlanDelivery.objects.get(plan=self.plan)
        self.assertEqual(delivery.telegram_chat_id, self.chat_id)
        pdf = [call for call in post.call_args_list if call.args[0] == "/internal/v1/plan-pdf/send"]
        self.assertEqual(len(pdf), 1)
        self.assertEqual(pdf[0].args[1]["plan_id"], self.plan.pk)
        self.assertEqual(self.client.post(f"/api/v1/planning/plans/{self.plan.pk}/publish/").status_code, 400)
        self.assertEqual(TelegramPlanDelivery.objects.count(), 1)

    def test_outbox_survives_bot_downtime_and_ack_is_scoped(self):
        with patch("apps.telegram.automation.bot_post_sync", side_effect=BotControlUnavailable()):
            with self.captureOnCommitCallbacks(execute=True):
                publish_pdf_after_commit(self.plan)
        pending = "/api/internal/v1/telegram/automation/plan-pdf/pending/"
        self.assertEqual(self.client.get(pending).status_code, 403)
        self.assertEqual(self.client.get(pending, **self.svc).data["recipients"][0]["plan_id"], self.plan.pk)
        ack = "/api/internal/v1/telegram/automation/plan-pdf/ack/"
        self.assertEqual(self.client.post(ack, {"plan_id": self.plan.pk, "telegram_chat_id": -999, "status": "DELIVERED"}, **self.svc).status_code, 404)
        self.assertEqual(self.client.post(ack, {"plan_id": self.plan.pk, "telegram_chat_id": self.chat_id, "status": "DELIVERED"}, **self.svc).status_code, 200)
        self.assertEqual(self.client.get(pending, **self.svc).data["recipients"], [])

    def test_rollback_creates_no_pdf_job(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                publish_pdf_after_commit(self.plan)
                raise RuntimeError("rollback")
        self.assertFalse(TelegramPlanDelivery.objects.exists())

    def test_pdf_identity_future_plan_and_no_private_binding_required(self):
        future = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=self.today + timedelta(days=7), end_date=self.today + timedelta(days=13), status=Plan.Status.PUBLISHED, published_at=timezone.now())
        url = f"/api/internal/v1/telegram/plan/{future.pk}/pdf/"
        with patch("apps.telegram.views_plan.export_plan_pdf", return_value=b"%PDF exact future plan") as render:
            response = self.client.get(url, {"telegram_chat_id": self.chat_id}, **self.svc)
            self.assertEqual(response.content, b"%PDF exact future plan")
            self.assertEqual(render.call_args.args[0].pk, future.pk)
        self.assertEqual(self.client.get(url, {"telegram_chat_id": -999}, **self.svc).status_code, 404)
        self.assertEqual(self.client.get(url, {"telegram_chat_id": self.chat_id}).status_code, 403)

    def test_reminder_without_plan_and_status_does_not_create_report(self):
        self.plan.status = Plan.Status.DRAFT
        self.plan.published_at = None
        self.plan.save()
        recipients = self.client.get("/api/internal/v1/telegram/automation/end-of-day-recipients/", {"date": self.today}, **self.svc)
        self.assertEqual(len(recipients.data["recipients"]), 1)
        check = self.client.get("/api/internal/v1/telegram/automation/report-status/", {"telegram_chat_id": self.chat_id, "telegram_user_id": self.user_id, "date": self.today}, **self.svc)
        self.assertFalse(check.data["finalized"])
        self.assertFalse(DailyReport.objects.exists())
