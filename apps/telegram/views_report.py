from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.accounts.models import StudentProfile
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.daily_reports.services import open_report, report_payload, report_today
from apps.daily_reports.views import DailyReportViewSet
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution

from .permissions import ServiceTokenPermission
from .views_plan import _resolve_student


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def report_today_view(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    try:
        report = open_report(student.user, today)
    except Exception as exc:
        from rest_framework.exceptions import ValidationError
        if isinstance(exc, ValidationError):
            return Response({"detail": exc.detail if hasattr(exc, 'detail') else str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        raise
    payload = report_payload(report)
    # Add unresolved items for finalize guard (reuse logic)
    items = list(PlanItem.objects.filter(plan_day__plan__student=student, plan_day__plan__status="PUBLISHED", plan_day__date=today).select_related("subject", "chapter", "topic").order_by("ordering", "pk"))
    exec_map = {e.plan_item_id: e for e in PlanItemExecution.objects.filter(student=student, plan_item_id__in=[i.pk for i in items])}
    unresolved = []
    for it in items:
        if it.kind == PlanItem.Kind.EVENT:
            continue
        st = exec_map.get(it.pk).status if exec_map.get(it.pk) else "NOT_STARTED"
        if st not in (PlanItemExecution.Status.COMPLETED, PlanItemExecution.Status.PARTIAL, PlanItemExecution.Status.NOT_DONE):
            unresolved.append({"id": it.pk, "title": it.title or (it.topic.name if it.topic_id else it.subject.name if it.subject_id else ""), "kind": it.kind})
    payload["unresolved_items"] = unresolved
    payload["finalized"] = bool(payload.get("closed_at"))
    payload["unresolved"] = unresolved
    return Response(payload)


@api_view(["PATCH"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def report_patch(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    try:
        report = open_report(student.user, today)
    except Exception as exc:
        from rest_framework.exceptions import ValidationError
        if isinstance(exc, ValidationError):
            return Response({"detail": exc.detail if hasattr(exc, 'detail') else str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        raise
    if report.closed_at:
        return Response({"detail": "گزارش امروز قبلاً نهایی شده است.", "code": "already_finalized"}, status=status.HTTP_409_CONFLICT)
    allowed = {"mobile_minutes", "self_rating", "note", "wake_time", "sleep_time"}
    data = {k: v for k, v in request.data.items() if k in allowed}
    # Use existing DailyFieldsSerializer logic
    from apps.daily_reports.serializers import DailyFieldsSerializer
    ser = DailyFieldsSerializer(report, data=data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(report_payload(report))


@api_view(["POST"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def report_finalize(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    # Find report
    try:
        report = DailyReport.objects.get(student=student, date=today)
    except DailyReport.DoesNotExist:
        return Response({"detail": "No report.", "code": "no_report"}, status=status.HTTP_404_NOT_FOUND)
    if report.closed_at:
        # Idempotent
        return Response({"detail": "Already finalized.", "closed_at": report.closed_at.isoformat()}, status=status.HTTP_200_OK)
    # Check unresolved
    items = list(PlanItem.objects.filter(plan_day__plan__student=student, plan_day__plan__status="PUBLISHED", plan_day__date=today).select_related("subject", "chapter", "topic"))
    exec_map = {e.plan_item_id: e for e in PlanItemExecution.objects.filter(student=student, plan_item_id__in=[i.pk for i in items])}
    unresolved = [it for it in items if it.kind != PlanItem.Kind.EVENT and (exec_map.get(it.pk).status if exec_map.get(it.pk) else None) not in (PlanItemExecution.Status.COMPLETED, PlanItemExecution.Status.PARTIAL, PlanItemExecution.Status.NOT_DONE)]
    if unresolved:
        return Response({"detail": "Unresolved items remain.", "code": "unresolved", "unresolved": [{"id": it.pk, "title": it.title or ""} for it in unresolved]}, status=status.HTTP_409_CONFLICT)
    # Validate self_rating/note required by CloseDaySerializer? But for Telegram we allow finalize even if not yet provided? Follow domain: close requires self_rating and note
    # Try to close using existing view logic — it expects date + self_rating + note
    # For Telegram, if not provided, use defaults? Instead call directly like the ViewSet does
    from apps.daily_reports.serializers import CloseDaySerializer
    # Request may include self_rating/note; if not, we need to require them
    date_str = request.data.get("date") or today.isoformat()
    payload = {"date": date_str}
    # If report already has self_rating/note, we can use them; else require from request
    if request.data.get("self_rating") is not None:
        payload["self_rating"] = request.data["self_rating"]
    elif report.self_rating is not None:
        payload["self_rating"] = report.self_rating
    else:
        return Response({"detail": "self_rating required to finalize.", "code": "missing_rating"}, status=status.HTTP_400_BAD_REQUEST)
    if request.data.get("note") is not None:
        payload["note"] = request.data["note"]
    elif report.note:
        payload["note"] = report.note
    else:
        payload["note"] = report.note or "روز خوبی بود."
        # Ensure note not empty — use fallback
    ser = CloseDaySerializer(data=payload)
    ser.is_valid(raise_exception=True)
    # Perform close like ViewSet
    report.self_rating = ser.validated_data["self_rating"]
    report.note = ser.validated_data["note"]
    report.closed_at = timezone.now()
    report.save(update_fields=["self_rating", "note", "closed_at", "updated_at"])
    return Response({"detail": "Finalized.", "closed_at": report.closed_at.isoformat(), "report": report_payload(report)})


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def report_yesterday(request):
    student, err = _resolve_student(request)
    if err:
        return err
    yesterday = report_today() - timedelta(days=1)
    try:
        report = DailyReport.objects.get(student=student, date=yesterday)
    except DailyReport.DoesNotExist:
        return Response({"date": yesterday.isoformat(), "has_report": False, "items": []})
    payload = report_payload(report)
    payload["finalized"] = bool(payload.get("closed_at"))
    return Response(payload)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def report_extra_activity(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    try:
        report = open_report(student.user, today)
    except Exception as exc:
        from rest_framework.exceptions import ValidationError
        if isinstance(exc, ValidationError):
            return Response({"detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        raise
    if report.closed_at:
        return Response({"detail": "گزارش امروز قبلاً نهایی شده است.", "code": "already_finalized"}, status=status.HTTP_409_CONFLICT)
    from apps.daily_reports.serializers import UnplannedItemInputSerializer
    ser = UnplannedItemInputSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    # Build DailyReportItem
    data = ser.validated_data
    subject = data.get("subject")
    if subject is not None:
        # Validate subject belongs to student's applicable Grade/Field (same rule as subjects endpoint)
        if not subject.is_active or not subject.field.is_active or not subject.field.grade.is_active:
            return Response({"detail": "Subject is not active.", "code": "invalid_subject"}, status=status.HTTP_400_BAD_REQUEST)
        if student.field_id:
            if subject.field_id != student.field_id:
                return Response({"detail": "Subject does not belong to student's field.", "code": "invalid_subject"}, status=status.HTTP_400_BAD_REQUEST)
        elif student.grade_id:
            if subject.field.grade_id != student.grade_id:
                return Response({"detail": "Subject does not belong to student's grade.", "code": "invalid_subject"}, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response({"detail": "Student has no grade/field; cannot assign subject.", "code": "invalid_subject"}, status=status.HTTP_400_BAD_REQUEST)
    # Map to model
    item = DailyReportItem(
        report=report,
        kind=data["kind"],
        title=data.get("title", ""),
        resource=data.get("resource", ""),
        start_time=data.get("start_time"),
        end_time=data.get("end_time"),
        subject=subject,
        chapter=data.get("chapter"),
        topic=data.get("topic"),
        actual_duration_minutes=data.get("actual_duration_minutes"),
        duration_source=data.get("duration_source", ""),
        actual_test_count=data.get("actual_test_count"),
        correct_count=data.get("correct_count"),
        wrong_count=data.get("wrong_count"),
        unanswered_count=data.get("unanswered_count"),
        ordering=report.items.count(),
        note=data.get("note", ""),
    )
    # Handle completion_status if present (not in Unplanned but handle)
    try:
        item.save()
    except Exception as exc:
        from django.core.exceptions import ValidationError as DjangoValidationError
        from rest_framework.exceptions import ValidationError as DRFValidationError
        if isinstance(exc, DjangoValidationError):
            return Response({"detail": exc.message_dict if hasattr(exc, 'message_dict') else str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, DRFValidationError):
            return Response({"detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        raise
    return Response(report_payload(report), status=status.HTTP_201_CREATED)
