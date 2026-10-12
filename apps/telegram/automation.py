"""Backend authority for automation eligibility; no business records in the bot."""
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.daily_reports.models import DailyReport
from apps.daily_reports.services import close_day_review
from apps.planning.models import Plan, PlanDay
from .models import TelegramStudentConnection, TelegramGroupConnection, TelegramPlanDelivery
from .permissions import ServiceTokenPermission
from .bot_control import bot_post_sync

logger = logging.getLogger(__name__)


def eligible_connections(date=None):
    connections = TelegramStudentConnection.objects.filter(
        is_active=True, is_enabled=True, is_suspended=False, is_banned=False,
        student__user__is_active=True,
        student__telegram_group_connections__is_active=True,
    ).select_related("student")
    if date is not None:
        connections = connections.filter(student_id__in=PlanDay.objects.filter(
            date=date, plan__status="PUBLISHED", plan__start_date__lte=date,
            plan__end_date__gte=date,
        ).values("plan__student_id"))
    return connections


def recipients(date, end_of_day=False):
    result = []
    for connection in eligible_connections(None if end_of_day else date):
        group = TelegramGroupConnection.objects.get(student=connection.student, is_active=True)
        row = {"telegram_chat_id": group.telegram_chat_id, "telegram_user_id": connection.telegram_user_id}
        if end_of_day:
            if DailyReport.objects.filter(student=connection.student, date=date, closed_at__isnull=False).exists():
                continue
            count = len(close_day_review(connection.student, date)["unresolved"])
            row["unresolved_count"] = count
        result.append(row)
    return result


def automation_response(request, end_of_day=False):
    date = serializers.DateField().run_validation(request.query_params.get("date"))
    return Response({"date": date.isoformat(), "recipients": recipients(date, end_of_day)})


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def morning_recipients(request):
    return automation_response(request)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def end_of_day_recipients(request):
    return automation_response(request, True)


def refresh_after_commit(plan):
    if plan.status != "PUBLISHED":
        return
    student_id = plan.student_id

    def refresh():
        # Recheck current eligibility after commit; failures must never roll back a save.
        try:
            connection = eligible_connections().filter(student_id=student_id).first()
            if connection is None:
                return
            group = TelegramGroupConnection.objects.get(student_id=student_id, is_active=True)
            result = bot_post_sync("/internal/v1/daily-plan/refresh", {
                "telegram_chat_id": group.telegram_chat_id,
                "telegram_user_id": connection.telegram_user_id,
            })
            if result.get("error"):
                logger.warning("Telegram plan refresh failed")
        except Exception:
            logger.warning("Telegram plan refresh unavailable; use manual resend")

    transaction.on_commit(refresh)


def publish_pdf_after_commit(plan):
    """Persist a publication before commit, then ask the bot to drain it."""
    group = TelegramGroupConnection.objects.filter(student=plan.student, is_active=True, student__user__is_active=True).first()
    if group is None:
        return
    delivery, _ = TelegramPlanDelivery.objects.get_or_create(plan=plan, defaults={"telegram_chat_id": group.telegram_chat_id})
    payload = _pdf_payload(delivery)
    def queue_pdf():
        try:
            bot_post_sync("/internal/v1/plan-pdf/send", payload)
        except Exception:
            # The scheduler also polls the persisted outbox, so downtime loses no publication.
            logger.warning("Published plan PDF queued for retry | plan_id=%s", plan.pk)
    transaction.on_commit(queue_pdf)


def _pdf_payload(delivery):
    from zoneinfo import ZoneInfo
    return {"plan_id": delivery.plan_id, "telegram_chat_id": delivery.telegram_chat_id,
            "delivery_date": delivery.plan.published_at.astimezone(ZoneInfo("Asia/Tehran")).date().isoformat()}


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def pending_plan_pdfs(request):
    deliveries = TelegramPlanDelivery.objects.filter(status="PENDING", plan__status="PUBLISHED").select_related("plan").order_by("pk")[:100]
    return Response({"recipients": [_pdf_payload(delivery) for delivery in deliveries]})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def acknowledge_plan_pdf(request):
    from django.shortcuts import get_object_or_404
    plan_id = serializers.IntegerField(min_value=1).run_validation(request.data.get("plan_id"))
    chat = serializers.IntegerField(max_value=-1).run_validation(request.data.get("telegram_chat_id"))
    status = serializers.ChoiceField(choices=["DELIVERED", "FAILED"]).run_validation(request.data.get("status"))
    delivery = get_object_or_404(TelegramPlanDelivery, plan_id=plan_id, telegram_chat_id=chat)
    # Late acknowledgments must not downgrade a confirmed successful delivery.
    if delivery.status != "DELIVERED":
        delivery.status = status
        delivery.delivered_at = timezone.now() if status == "DELIVERED" else None
        delivery.save(update_fields=["status", "delivered_at"])
    return Response({"status": delivery.status})


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def report_status(request):
    from .views_plan import _resolve_student
    student, error = _resolve_student(request)
    if error is not None:
        return error
    date = serializers.DateField().run_validation(request.query_params.get("date"))
    return Response({"date": date.isoformat(), "finalized": DailyReport.objects.filter(student=student, date=date, closed_at__isnull=False).exists(), "unresolved": close_day_review(student, date)["unresolved"]})
