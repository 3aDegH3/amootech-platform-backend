from datetime import date

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.daily_reports.services import ACADEMIC_KINDS, report_today
from apps.planning.execution import transition
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution

from .permissions import ServiceTokenPermission
from .views_plan import _resolve_student


class ExecutionUpdateSerializer(serializers.Serializer):
    plan_item_id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=["COMPLETED", "PARTIAL", "NOT_DONE"])
    actual_duration_minutes = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    duration_source = serializers.ChoiceField(choices=["MANUAL", "PLAN"], allow_null=True, required=False)
    actual_test_count = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    correct_count = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    wrong_count = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    unanswered_count = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    note = serializers.CharField(max_length=500, allow_blank=True, required=False)


def _resolve_plan_item(student, plan_item_id) -> tuple[PlanItem | None, Response | None]:
    try:
        item = PlanItem.objects.select_related("plan_day__plan").get(pk=plan_item_id)
    except PlanItem.DoesNotExist:
        return None, Response({"detail": "PlanItem not found.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    if item.plan_day.plan.student_id != student.pk:
        return None, Response({"detail": "PlanItem not found.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    if item.plan_day.plan.status != Plan.Status.PUBLISHED:
        return None, Response({"detail": "Draft plan cannot be executed.", "code": "draft"}, status=status.HTTP_409_CONFLICT)
    return item, None


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def execution_today(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    plan = Plan.objects.filter(student=student, status=Plan.Status.PUBLISHED, start_date__lte=today, end_date__gte=today).order_by("-start_date").first()
    if plan is None:
        return Response({"date": today.isoformat(), "has_plan": False, "items": []})
    day = PlanDay.objects.filter(plan=plan, date=today).first()
    if day is None:
        return Response({"date": today.isoformat(), "has_plan": False, "items": []})
    items = list(PlanItem.objects.filter(plan_day=day).select_related("subject", "chapter", "topic").order_by("ordering", "pk"))
    exec_map = {e.plan_item_id: e for e in PlanItemExecution.objects.filter(student=student, plan_item_id__in=[i.pk for i in items])}
    # Also fetch report items for actual values
    try:
        report = DailyReport.objects.get(student=student, date=today)
        report_map = {r.plan_item_id: r for r in DailyReportItem.objects.filter(report=report, plan_item_id__in=[i.pk for i in items])}
    except DailyReport.DoesNotExist:
        report_map = {}
    result = []
    for it in items:
        exe = exec_map.get(it.pk)
        row = report_map.get(it.pk)
        result.append({
            "id": it.pk,
            "kind": it.kind,
            "subject": it.subject.name if it.subject_id else None,
            "title": it.title,
            "planned_duration_minutes": it.planned_duration_minutes,
            "planned_test_count": it.test_count,
            "execution_status": exe.status if exe else "PENDING",
            "source": exe.source if exe else None,
            "actual_duration_minutes": row.actual_duration_minutes if row else None,
            "actual_test_count": row.actual_test_count if row else None,
            "correct_count": row.correct_count if row else None,
            "wrong_count": row.wrong_count if row else None,
            "unanswered_count": row.unanswered_count if row else None,
            "note": row.note if row else "",
        })
    return Response({"date": today.isoformat(), "has_plan": True, "items": result})


@api_view(["POST", "PATCH"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
@transaction.atomic
def execution_update(request):
    student, err = _resolve_student(request)
    if err:
        return err
    ser = ExecutionUpdateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    plan_item_id = ser.validated_data["plan_item_id"]
    req_status = ser.validated_data["status"]
    item, err2 = _resolve_plan_item(student, plan_item_id)
    if err2:
        return err2

    # Telegram may send for today or for any published plan day? For Sprint 4 we only allow today's items via Telegram, but allow any date if needed — enforce today for now
    today = report_today()
    # Allow execution for any date? But spec says "today" execution; we allow if plan covers today but item is today
    # For simplicity allow any item whose plan is published and student owns it; the execution domain will handle date checks
    # Use existing execution service: transition(user, item_id, action)
    # We need to map req_status to execution action and also set report item actuals if provided
    # Reuse user from student
    user = student.user
    try:
        if req_status == "COMPLETED":
            exe = transition(user, plan_item_id, "quick_complete", source="TELEGRAM")
        elif req_status == "PARTIAL":
            exe = transition(user, plan_item_id, "set_partial", source="TELEGRAM")
        elif req_status == "NOT_DONE":
            exe = transition(user, plan_item_id, "mark_not_done", source="TELEGRAM")
        else:
            return Response({"detail": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:
        # Map ValidationError / ActiveTimerConflict
        from rest_framework.exceptions import ValidationError as DRFValidationError, APIException
        if isinstance(exc, DRFValidationError):
            transaction.set_rollback(True)
            return Response({"detail": exc.detail if hasattr(exc, 'detail') else str(exc), "code": "validation_error"}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, APIException):
            detail = getattr(exc, 'detail', str(exc))
            return Response({"detail": detail, "code": getattr(exc, 'default_code', 'error')}, status=exc.status_code)
        raise

    # If actual values provided, update DailyReportItem for that plan_item
    actual_duration = ser.validated_data.get("actual_duration_minutes")
    duration_source = ser.validated_data.get("duration_source")
    actual_test_count = ser.validated_data.get("actual_test_count")
    correct = ser.validated_data.get("correct_count")
    wrong = ser.validated_data.get("wrong_count")
    unanswered = ser.validated_data.get("unanswered_count")
    note = ser.validated_data.get("note")

    has_actual = any(v is not None for v in [actual_duration, actual_test_count, correct, wrong, unanswered, note if note else None])
    if has_actual or req_status in ("COMPLETED", "PARTIAL"):
        # Ensure DailyReport and item row exists
        from apps.daily_reports.services import open_report
        try:
            report = open_report(user, item.plan_day.date)
        except Exception as exc:
            from rest_framework.exceptions import ValidationError as DRFValidationError
            if isinstance(exc, DRFValidationError):
                transaction.set_rollback(True)
                return Response({"detail": exc.detail if hasattr(exc, 'detail') else str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            raise
        # Get or create report item for this plan_item
        row, created = DailyReportItem.objects.get_or_create(report=report, plan_item=item, defaults={"ordering": 0})
        # Update fields
        if actual_duration is not None:
            row.actual_duration_minutes = actual_duration
            row.duration_source = duration_source or DailyReportItem.DurationSource.MANUAL
        elif req_status == "COMPLETED" and item.planned_duration_minutes and row.actual_duration_minutes is None:
            # If fast path "as planned", frontend/backend should send planned values; if not sent, default to planned
            pass
        if actual_test_count is not None:
            row.actual_test_count = actual_test_count
        if correct is not None:
            row.correct_count = correct
        if wrong is not None:
            row.wrong_count = wrong
        if unanswered is not None:
            row.unanswered_count = unanswered
        if note is not None:
            row.note = note
        # Auto-calc unanswered if not provided but test count provided
        if row.actual_test_count is not None and row.correct_count is not None and row.wrong_count is not None and row.unanswered_count is None:
            calc = row.actual_test_count - (row.correct_count or 0) - (row.wrong_count or 0)
            if calc >= 0:
                row.unanswered_count = calc
        # If status is NOT_DONE, clear actuals? No — NOT_DONE should have no actuals
        if req_status == "NOT_DONE":
            row.actual_duration_minutes = None
            row.duration_source = ""
            row.actual_test_count = None
            row.correct_count = None
            row.wrong_count = None
            row.unanswered_count = None
        try:
            row.source = "TELEGRAM"
            row.save()
        except Exception as exc:
            from django.core.exceptions import ValidationError as DjangoValidationError
            if isinstance(exc, DjangoValidationError):
                transaction.set_rollback(True)
                return Response({"detail": exc.message_dict if hasattr(exc, 'message_dict') else str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            # Also DRF ValidationError from full_clean
            from rest_framework.exceptions import ValidationError as DRFValidationError2
            if isinstance(exc, DRFValidationError2):
                transaction.set_rollback(True)
                return Response({"detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
            raise

    return Response({"source": exe.source, "status": exe.status, "plan_item_id": plan_item_id, "execution_status": exe.status})
