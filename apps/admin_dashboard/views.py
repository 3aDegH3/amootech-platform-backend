from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction

from apps.accounts.models import CounselorProfile, StudentProfile, User
from apps.accounts.permissions import IsAdmin as AccountsIsAdmin
from apps.planning.models import Plan
from apps.daily_reports.models import DailyReport
from apps.telegram.models import TelegramGroupConnection, TelegramStudentConnection, TelegramAdminActionLog
from apps.telegram.bot_control import bot_reachable_sync
from .permissions import IsAdmin


def _today():
    return timezone.localdate()


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def dashboard_summary(request):
    today = _today()
    seven_ago = today - timedelta(days=6)
    thirty_ago = today - timedelta(days=29)

    # Students
    total_students = StudentProfile.objects.count()
    active_students = StudentProfile.objects.filter(user__is_active=True).count()
    inactive_students = total_students - active_students
    unassigned = StudentProfile.objects.filter(counselor__isnull=True).count()
    new_7d = StudentProfile.objects.filter(user__date_joined__date__gte=seven_ago).count()
    new_30d = StudentProfile.objects.filter(user__date_joined__date__gte=thirty_ago).count()

    # Counselors
    total_counselors = CounselorProfile.objects.count()
    active_counselors = CounselorProfile.objects.filter(user__is_active=True).count()

    # Plans
    total_plans = Plan.objects.count()
    drafts = Plan.objects.filter(status=Plan.Status.DRAFT).count()
    published = Plan.objects.filter(status=Plan.Status.PUBLISHED).count()
    week_start = today - timedelta(days=today.weekday())
    plans_this_week = Plan.objects.filter(created_at__date__gte=week_start).count()
    # Current plans (active today)
    current_published = Plan.objects.filter(status=Plan.Status.PUBLISHED, start_date__lte=today, end_date__gte=today).count()
    # Students without active plan
    students_with_current = Plan.objects.filter(
        status=Plan.Status.PUBLISHED, start_date__lte=today, end_date__gte=today
    ).values_list("student_id", flat=True).distinct()
    without_current_plan = StudentProfile.objects.exclude(pk__in=students_with_current).count() if total_students else 0

    # Reports today
    reports_today = DailyReport.objects.filter(date=today).count()
    reporting_rate = round(reports_today * 100 / total_students, 1) if total_students else 0

    # Telegram
    connected_groups = TelegramGroupConnection.objects.filter(is_active=True).count()
    connected_students = TelegramStudentConnection.objects.filter(is_active=True).count()
    disconnected = total_students - connected_groups if total_students else 0
    suspended = TelegramStudentConnection.objects.filter(is_active=True, is_suspended=True).count()
    banned = TelegramStudentConnection.objects.filter(is_active=True, is_banned=True).count()
    disabled = TelegramStudentConnection.objects.filter(is_active=True, is_enabled=False).count()
    # Locked groups: need bot telemetry - approximate via admin logs? Use 0 if unavailable
    # For now derive from recent lock actions not unlocked - simpler: count from bot if we had snapshot
    # We'll report 0 and let frontend handle
    recent_errors = TelegramAdminActionLog.objects.filter(success=False, created_at__date__gte=seven_ago).count()
    bot_reachable = bot_reachable_sync()

    # Trends: last 7 days student registrations
    trends_students = []
    for i in range(7):
        d = seven_ago + timedelta(days=i)
        cnt = StudentProfile.objects.filter(user__date_joined__date=d).count()
        trends_students.append({"date": d.isoformat(), "count": cnt})

    trends_plans = []
    for i in range(7):
        d = seven_ago + timedelta(days=i)
        cnt = Plan.objects.filter(created_at__date=d).count()
        trends_plans.append({"date": d.isoformat(), "count": cnt})

    trends_reports = []
    for i in range(7):
        d = seven_ago + timedelta(days=i)
        cnt = DailyReport.objects.filter(date=d).count()
        trends_reports.append({"date": d.isoformat(), "count": cnt})

    # Counselor workload for dashboard preview
    counselor_workload = list(
        CounselorProfile.objects.select_related("user")
        .annotate(
            students_count=Count("students", distinct=True),
            active_students=Count("students", filter=Q(students__user__is_active=True), distinct=True),
        )
        .order_by("-students_count")[:5]
        .values("id", "user__username", "user__first_name", "user__last_name", "students_count", "active_students")
    )

    # Alerts: derive from real data, no fake
    alerts = []
    if unassigned > 0:
        alerts.append({"type": "unassigned", "severity": "warning", "message": f"{unassigned} دانش‌آموز بدون مشاور", "count": unassigned})
    if without_current_plan > 0:
        alerts.append({"type": "no_plan", "severity": "warning", "message": f"{without_current_plan} دانش‌آموز بدون برنامه فعال", "count": without_current_plan})
    if disconnected > 0:
        alerts.append({"type": "telegram_disconnected", "severity": "info", "message": f"{disconnected} دانش‌آموز گروه تلگرام متصل ندارد", "count": disconnected})
    if suspended > 0:
        alerts.append({"type": "suspended", "severity": "danger", "message": f"{suspended} اتصال تلگرام تعلیق شده", "count": suspended})
    if banned > 0:
        alerts.append({"type": "banned", "severity": "danger", "message": f"{banned} کاربر بن شده", "count": banned})
    if drafts > 0:
        alerts.append({"type": "drafts", "severity": "info", "message": f"{drafts} برنامه پیش‌نویس منتشر نشده", "count": drafts})
    if recent_errors > 0:
        alerts.append({"type": "bot_errors", "severity": "danger", "message": f"{recent_errors} خطای بات در ۷ روز اخیر", "count": recent_errors})
    # Students without report today (if has current plan but no report)
    reported_today_ids = set(DailyReport.objects.filter(date=today).values_list("student_id", flat=True))
    not_reported = total_students - len(reported_today_ids) if total_students else 0
    if not_reported > 0 and total_students > 0:
        # Only alert if significant
        if not_reported > total_students * 0.5:
            alerts.append({"type": "no_report", "severity": "warning", "message": f"{not_reported} دانش‌آموز امروز گزارش ثبت نکرده", "count": not_reported})

    return Response({
        "students": {"total": total_students, "active": active_students, "inactive": inactive_students, "unassigned": unassigned, "new_7d": new_7d, "new_30d": new_30d, "without_current_plan": without_current_plan},
        "counselors": {"total": total_counselors, "active": active_counselors},
        "plans": {"total": total_plans, "drafts": drafts, "published": published, "this_week": plans_this_week, "current_published": current_published},
        "reports": {"today_count": reports_today, "today_rate": reporting_rate, "not_reported_today": not_reported},
        "telegram": {"connected_groups": connected_groups, "connected_students": connected_students, "disconnected": max(0, disconnected), "suspended": suspended, "banned": banned, "disabled": disabled, "recent_errors": recent_errors, "bot_reachable": bot_reachable},
        "trends": {"students_7d": trends_students, "plans_7d": trends_plans, "reports_7d": trends_reports},
        "workload_preview": counselor_workload,
        "alerts": alerts,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def telegram_summary(request):
    total_students = StudentProfile.objects.count()
    connected_groups = TelegramGroupConnection.objects.filter(is_active=True).select_related("student__user", "student__counselor__user").count()
    connected_students = TelegramStudentConnection.objects.filter(is_active=True).count()
    suspended = TelegramStudentConnection.objects.filter(is_active=True, is_suspended=True).count()
    banned = TelegramStudentConnection.objects.filter(is_active=True, is_banned=True).count()
    disabled = TelegramStudentConnection.objects.filter(is_active=True, is_enabled=False).count()
    bot_reachable = bot_reachable_sync()
    recent_logs = list(
        TelegramAdminActionLog.objects.select_related("actor", "student__user")
        .order_by("-created_at")[:10]
        .values("id", "action", "success", "reason", "created_at", "actor__username", "student__user__username")
    )
    for r in recent_logs:
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
    return Response({
        "total_students": total_students,
        "connected_groups": connected_groups,
        "connected_students": connected_students,
        "disconnected": max(0, total_students - connected_groups),
        "suspended": suspended,
        "banned": banned,
        "disabled": disabled,
        "bot_reachable": bot_reachable,
        "recent_logs": recent_logs,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def counselor_summary(request, counselor_id: int):
    try:
        counselor = CounselorProfile.objects.select_related("user").get(pk=counselor_id)
    except CounselorProfile.DoesNotExist:
        return Response({"detail": "مشاور یافت نشد."}, status=status.HTTP_404_NOT_FOUND)
    students = StudentProfile.objects.filter(counselor=counselor).select_related("user", "grade", "field")
    students_count = students.count()
    active_students = students.filter(user__is_active=True).count()
    plans = Plan.objects.filter(counselor=counselor)
    drafts = plans.filter(status=Plan.Status.DRAFT).count()
    published = plans.filter(status=Plan.Status.PUBLISHED).count()
    today = _today()
    reports_today = DailyReport.objects.filter(student__counselor=counselor, date=today).count()
    # Recent plans
    recent_plans = list(plans.select_related("student__user").order_by("-created_at")[:5].values("id", "student__user__username", "title", "status", "start_date", "end_date", "created_at"))
    for r in recent_plans:
        r["start_date"] = r["start_date"].isoformat() if r["start_date"] else None
        r["end_date"] = r["end_date"].isoformat() if r["end_date"] else None
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
    student_list = list(students.values("id", "user__username", "user__first_name", "user__last_name", "user__is_active", "grade__name", "field__name", "school_name", "user__date_joined"))
    for s in student_list:
        s["date_joined"] = s["user__date_joined"].isoformat() if s["user__date_joined"] else None
    return Response({
        "counselor": {"id": counselor.pk, "username": counselor.user.username, "first_name": counselor.user.first_name, "last_name": counselor.user.last_name, "is_active": counselor.user.is_active, "date_joined": counselor.user.date_joined.isoformat() if counselor.user.date_joined else None},
        "students_count": students_count,
        "active_students": active_students,
        "plans": {"total": plans.count(), "drafts": drafts, "published": published},
        "reports_today": reports_today,
        "recent_plans": recent_plans,
        "students": student_list,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def bulk_assign(request):
    student_ids = request.data.get("student_ids")
    counselor_id = request.data.get("counselor_id")  # None for unassign
    if not isinstance(student_ids, list) or not student_ids:
        return Response({"detail": "student_ids باید آرایه‌ای از شناسه‌ها باشد."}, status=status.HTTP_400_BAD_REQUEST)
    if len(student_ids) > 100:
        return Response({"detail": "حداکثر ۱۰۰ دانش‌آموز در هر درخواست."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        student_ids = [int(x) for x in student_ids]
    except (TypeError, ValueError):
        return Response({"detail": "شناسه دانش‌آموز نامعتبر."}, status=status.HTTP_400_BAD_REQUEST)
    counselor = None
    if counselor_id is not None:
        try:
            counselor_id = int(counselor_id)
            counselor = CounselorProfile.objects.select_related("user").get(pk=counselor_id)
            if not counselor.user.is_active:
                return Response({"detail": "مشاور غیرفعال است."}, status=status.HTTP_400_BAD_REQUEST)
        except (ValueError, CounselorProfile.DoesNotExist):
            return Response({"detail": "مشاور یافت نشد."}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        qs = StudentProfile.objects.select_for_update().filter(pk__in=student_ids)
        found_ids = set(qs.values_list("pk", flat=True))
        missing = set(student_ids) - found_ids
        if missing:
            return Response({"detail": f"دانش‌آموزان یافت نشد: {sorted(missing)}"}, status=status.HTTP_404_NOT_FOUND)
        updated = qs.update(counselor=counselor)
    return Response({"updated": updated, "counselor_id": counselor.pk if counselor else None})


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def recent_activity(request):
    logs = list(
        TelegramAdminActionLog.objects.select_related("actor", "student__user")
        .order_by("-created_at")[:30]
        .values("id", "action", "success", "reason", "created_at", "actor__username", "student_id", "student__user__username")
    )
    for r in logs:
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
    # Also include recent plan publishes/creates via Plan table
    recent_plans = list(
        Plan.objects.select_related("student__user", "counselor__user")
        .order_by("-updated_at")[:10]
        .values("id", "title", "status", "student__user__username", "counselor__user__username", "updated_at", "created_at")
    )
    for r in recent_plans:
        r["updated_at"] = r["updated_at"].isoformat() if r["updated_at"] else None
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
    return Response({"telegram_logs": logs, "recent_plans": recent_plans})
