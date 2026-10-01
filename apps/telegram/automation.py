"""Backend authority for automation eligibility; no business records in the bot."""
import logging

from django.db import transaction
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.daily_reports.models import DailyReport
from apps.daily_reports.services import close_day_review
from apps.planning.models import PlanDay
from .models import TelegramStudentConnection, TelegramGroupConnection
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
    for connection in eligible_connections(date):
        group = TelegramGroupConnection.objects.get(student=connection.student, is_active=True)
        row = {"telegram_chat_id": group.telegram_chat_id, "telegram_user_id": connection.telegram_user_id}
        if end_of_day:
            if DailyReport.objects.filter(student=connection.student, date=date, closed_at__isnull=False).exists():
                continue
            count = len(close_day_review(connection.student, date)["unresolved"])
            if not count:
                continue
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
