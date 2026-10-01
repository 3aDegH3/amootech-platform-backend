from datetime import date, timedelta

from django.db.models import Prefetch
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.accounts.models import StudentProfile
from apps.daily_reports.models import DailyReportItem
from apps.daily_reports.services import ACADEMIC_KINDS, report_today
from apps.planning.export_service import export_plan_excel, export_plan_pdf
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution

from .models import TelegramGroupConnection, TelegramStudentConnection
from .permissions import ServiceTokenPermission


def _resolve_student(request) -> tuple[StudentProfile | None, Response | None]:
    """Resolve student from telegram_chat_id + telegram_user_id. Returns (student, error_response)."""
    # Allow both query_params and JSON body
    chat_raw = request.query_params.get("telegram_chat_id") or (request.data.get("telegram_chat_id") if hasattr(request, "data") else None)
    user_raw = request.query_params.get("telegram_user_id") or (request.data.get("telegram_user_id") if hasattr(request, "data") else None)
    if chat_raw is None or user_raw is None:
        return None, Response({"detail": "telegram_chat_id and telegram_user_id are required.", "code": "missing_identity"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        chat_id = int(chat_raw)
        user_id = int(user_raw)
    except (ValueError, TypeError):
        return None, Response({"detail": "Invalid telegram ids.", "code": "invalid_identity"}, status=status.HTTP_400_BAD_REQUEST)
    # Group must exist and be active
    try:
        group = TelegramGroupConnection.objects.select_related("student__user").get(telegram_chat_id=chat_id, is_active=True)
    except TelegramGroupConnection.DoesNotExist:
        return None, Response({"detail": "حساب تلگرام شما هنوز به Amootech متصل نشده است.", "code": "unknown_group"}, status=status.HTTP_403_FORBIDDEN)
    student = group.student
    # User must be bound to same student and active
    try:
        conn = TelegramStudentConnection.objects.get(student=student, is_active=True)
    except TelegramStudentConnection.DoesNotExist:
        return None, Response({"detail": "حساب تلگرام شما هنوز به Amootech متصل نشده است.", "code": "not_connected"}, status=status.HTTP_403_FORBIDDEN)
    if conn.telegram_user_id != user_id:
        return None, Response({"detail": "حساب تلگرام شما به این گروه متصل نیست.", "code": "user_mismatch"}, status=status.HTTP_403_FORBIDDEN)
    # Student active check
    if not student.user.is_active:
        return None, Response({"detail": "Student inactive.", "code": "inactive"}, status=status.HTTP_403_FORBIDDEN)
    if not conn.is_active or not group.is_active:
        return None, Response({"detail": "Connection inactive.", "code": "inactive"}, status=status.HTTP_403_FORBIDDEN)
    # Central access guard — keep this after _resolve_student's identity checks so ordering stays stable
    if not conn.is_enabled:
        return None, Response({"detail": "دسترسی تلگرام شما در حال حاضر غیرفعال است.", "code": "telegram_disabled"}, status=status.HTTP_403_FORBIDDEN)
    if conn.is_suspended:
        return None, Response({"detail": "دسترسی تلگرام شما موقتاً تعلیق شده است.", "code": "telegram_suspended"}, status=status.HTTP_403_FORBIDDEN)
    if conn.is_banned:
        return None, Response({"detail": "دسترسی تلگرام شما محدود شده است.", "code": "telegram_banned"}, status=status.HTTP_403_FORBIDDEN)
    return student, None


def _execution_status_map(execution: PlanItemExecution | None) -> str:
    if execution is None:
        return "PENDING"
    s = execution.status
    if s == PlanItemExecution.Status.COMPLETED:
        return "COMPLETED"
    if s == PlanItemExecution.Status.PARTIAL:
        return "PARTIAL"
    if s == PlanItemExecution.Status.NOT_DONE:
        return "NOT_DONE"
    # IN_PROGRESS, PAUSED are pending from telegram read perspective
    return "PENDING"


def _day_payload(student: StudentProfile, target_date: date) -> dict:
    plan_qs = Plan.objects.filter(student=student, status=Plan.Status.PUBLISHED, start_date__lte=target_date, end_date__gte=target_date).order_by("-start_date", "-pk")
    plan = plan_qs.first()
    if plan is None:
        return {"date": target_date.isoformat(), "has_plan": False, "plan": None, "summary": _empty_summary(), "items": []}
    day = PlanDay.objects.filter(plan=plan, date=target_date).first()
    if day is None:
        return {"date": target_date.isoformat(), "has_plan": False, "plan": {"id": plan.pk, "title": plan.title, "status": plan.status}, "summary": _empty_summary(), "items": []}
    items = list(PlanItem.objects.filter(plan_day=day).select_related("subject", "chapter", "topic").order_by("ordering", "pk"))
    exec_map = {e.plan_item_id: e for e in PlanItemExecution.objects.filter(student=student, plan_item_id__in=[i.pk for i in items])}
    report_map = {}
    # Also check DailyReportItem for manual data? Execution is source of truth for status; report items overlay actual_test later if needed
    payload_items = []
    completed = partial = not_done = pending = 0
    actionable = 0
    for it in items:
        exe = exec_map.get(it.pk)
        st = _execution_status_map(exe)
        if it.kind in ACADEMIC_KINDS:
            actionable += 1
            if st == "COMPLETED":
                completed += 1
            elif st == "PARTIAL":
                partial += 1
            elif st == "NOT_DONE":
                not_done += 1
            else:
                pending += 1
        else:
            # EVENT also counts as pending if not completed? For telegram we keep pending
            if st == "COMPLETED":
                completed += 1
            elif st == "PARTIAL":
                partial += 1
            elif st == "NOT_DONE":
                not_done += 1
            else:
                pending += 1
            actionable += 1 if st in ("COMPLETED", "PARTIAL", "NOT_DONE", "PENDING") else 0
        payload_items.append({
            "id": it.pk,
            "order": it.ordering,
            "type": it.kind,
            "subject": it.subject.name if it.subject_id else None,
            "chapter": it.chapter.name if it.chapter_id else None,
            "topic": it.topic.name if it.topic_id else None,
            "planned_duration_minutes": it.planned_duration_minutes,
            "planned_test_count": it.test_count,
            "start_time": it.start_time.isoformat(timespec="minutes") if it.start_time else None,
            "end_time": it.end_time.isoformat(timespec="minutes") if it.end_time else None,
            "description": it.note or "",
            "execution_status": st,
        })
    total = len(items)
    summary = {
        "total_items": total,
        "completed": completed,
        "partial": partial,
        "not_completed": not_done,
        "pending": pending,
        "completion_percentage": round(completed * 100 / actionable) if actionable else (0 if total else None),
    }
    return {
        "date": target_date.isoformat(),
        "has_plan": True,
        "plan": {"id": plan.pk, "title": plan.title, "status": plan.status},
        "summary": summary,
        "items": payload_items,
    }


def _empty_summary():
    return {"total_items": 0, "completed": 0, "partial": 0, "not_completed": 0, "pending": 0, "completion_percentage": None}


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def plan_today(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    return Response(_day_payload(student, today))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def plan_tomorrow(request):
    student, err = _resolve_student(request)
    if err:
        return err
    tomorrow = report_today() + timedelta(days=1)
    return Response(_day_payload(student, tomorrow))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def plan_by_date(request):
    student, err = _resolve_student(request)
    if err:
        return err
    raw = request.query_params.get("date")
    if not raw:
        return Response({"detail": "date is required (YYYY-MM-DD).", "code": "missing_date"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        target = date.fromisoformat(raw)
    except ValueError:
        return Response({"detail": "Invalid date.", "code": "invalid_date"}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_day_payload(student, target))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def plan_week(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    # Find published plan covering today, else most recent published
    plan = Plan.objects.filter(student=student, status=Plan.Status.PUBLISHED, start_date__lte=today, end_date__gte=today).order_by("-start_date").first()
    if plan is None:
        plan = Plan.objects.filter(student=student, status=Plan.Status.PUBLISHED).order_by("-start_date").first()
    if plan is None:
        return Response({"days": []})
    days_qs = PlanDay.objects.filter(plan=plan).order_by("date")
    # Prefetch items
    items_by_day = {}
    for item in PlanItem.objects.filter(plan_day__plan=plan).order_by("ordering"):
        items_by_day.setdefault(item.plan_day_id, []).append(item)
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    # Persian weekday labels? Keep iso date and provide weekday index
    result = []
    for d in days_qs:
        items = items_by_day.get(d.pk, [])
        total_minutes = sum(i.planned_duration_minutes or 0 for i in items)
        total_tests = sum(i.test_count or 0 for i in items)
        result.append({
            "date": d.date.isoformat(),
            "weekday": d.date.weekday(),
            "weekday_label": weekdays[d.date.weekday()],
            "total_items": len(items),
            "total_planned_duration_minutes": total_minutes,
            "total_planned_tests": total_tests,
        })
    return Response({"days": result, "plan": {"id": plan.pk, "title": plan.title}})


def _current_published_plan(student: StudentProfile) -> Plan | None:
    today = report_today()
    plan = Plan.objects.filter(student=student, status=Plan.Status.PUBLISHED, start_date__lte=today, end_date__gte=today).order_by("-start_date").first()
    if plan is None:
        plan = Plan.objects.filter(student=student, status=Plan.Status.PUBLISHED).order_by("-start_date").first()
    return plan


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def plan_pdf(request):
    student, err = _resolve_student(request)
    if err:
        return err
    plan = _current_published_plan(student)
    if plan is None:
        return Response({"detail": "No published plan.", "code": "no_plan"}, status=status.HTTP_404_NOT_FOUND)
    data = export_plan_pdf(plan)
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="plan-{plan.pk}.pdf"'
    return resp


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def plan_excel(request):
    student, err = _resolve_student(request)
    if err:
        return err
    plan = _current_published_plan(student)
    if plan is None:
        return Response({"detail": "No published plan.", "code": "no_plan"}, status=status.HTTP_404_NOT_FOUND)
    data = export_plan_excel(plan)
    resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="plan-{plan.pk}.xlsx"'
    return resp
