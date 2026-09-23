"""Small, serialized state machine for student execution of published plan items."""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from apps.accounts.models import StudentProfile
from .models import PlanItem, PlanItemExecution


class ActiveTimerConflict(APIException):
    status_code = 409
    default_code = "active_timer_conflict"


def student_item(user, item_id):
    item = PlanItem.objects.select_related("plan_day__plan").filter(
        pk=item_id,
        plan_day__plan__student__user=user,
        plan_day__plan__status="PUBLISHED",
    ).first()
    if item is None:
        from rest_framework.exceptions import NotFound
        raise NotFound("This published plan item is not available to you.")
    return item


def active_conflict(student, item):
    active = PlanItemExecution.objects.select_related("plan_item__subject").filter(
        student=student, status=PlanItemExecution.Status.IN_PROGRESS,
    ).exclude(plan_item=item).first()
    if active:
        name = active.plan_item.title or (active.plan_item.subject.name if active.plan_item.subject_id else active.plan_item.get_kind_display())
        raise ActiveTimerConflict({
            "detail": f"یک فعالیت دیگر در حال اجراست: {name}",
            "active_item": active.plan_item_id,
        })


@transaction.atomic
def transition(user, item_id, action, *, complete=None):
    item = student_item(user, item_id)
    # Lock the owner row before looking for execution records. This also serializes
    # starts of different items when neither item has an execution row yet.
    student = StudentProfile.objects.select_for_update().get(user=user)
    execution = PlanItemExecution.objects.select_for_update().filter(plan_item=item).first()
    now = timezone.now()
    status = PlanItemExecution.Status

    if action == "start":
        from apps.daily_reports.models import DailyReport
        if DailyReport.objects.filter(student=student, date=item.plan_day.date, closed_at__isnull=False).exists():
            raise ValidationError({"detail": "روز پایان یافته است. برای تغییر عملکرد، گزارش امروز را ویرایش کنید."})
        if item.kind == PlanItem.Kind.EVENT:
            raise ValidationError({"detail": "برای رویداد از «انجام شد» استفاده کنید."})
        if execution is None:
            active_conflict(student, item)
            return PlanItemExecution.objects.create(
                student=student, plan_item=item, status=status.IN_PROGRESS,
                started_at=now, current_session_started_at=now,
            )
        if execution.status == status.IN_PROGRESS:
            return execution  # Repeated Start is harmless.
        raise ValidationError({"detail": "این فعالیت قبلاً شروع شده است. برای ادامه از «ادامه» استفاده کنید."})

    if action == "quick_complete":
        if execution is None:
            return PlanItemExecution.objects.create(
                student=student, plan_item=item, status=status.COMPLETED, completed_at=now,
            )
        if execution.status == status.COMPLETED:
            return execution  # Repeated click is harmless.
        if execution.status == status.IN_PROGRESS:
            execution.accumulated_seconds = execution.elapsed_seconds(now)
        execution.current_session_started_at = None
        execution.completed_at = now
        execution.status = status.COMPLETED
        execution.save(update_fields=("status", "accumulated_seconds", "current_session_started_at", "completed_at"))
        return execution

    if action == "set_partial":
        if execution is None:
            return PlanItemExecution.objects.create(student=student, plan_item=item, status=status.PARTIAL, completed_at=now)
        if execution.status == status.IN_PROGRESS:
            execution.accumulated_seconds = execution.elapsed_seconds(now)
        execution.current_session_started_at = None
        execution.completed_at = now
        execution.status = status.PARTIAL
        execution.save(update_fields=("status", "accumulated_seconds", "current_session_started_at", "completed_at"))
        return execution

    if action == "mark_not_done":
        from apps.daily_reports.models import DailyReportItem
        has_performance = DailyReportItem.objects.filter(plan_item=item, report__student=student).exclude(
            actual_duration_minutes__isnull=True, actual_test_count__isnull=True,
        ).exists()
        if execution and (execution.status == status.IN_PROGRESS or execution.elapsed_seconds(now) > 0):
            raise ValidationError({"detail": "برای فعالیت زمان‌دار، زمان واقعی را ثبت و وضعیت ناقص را انتخاب کنید."})
        if has_performance:
            raise ValidationError({"detail": "برای این فعالیت عملکرد ثبت شده است. ابتدا عملکرد را ویرایش کنید."})
        if execution is None:
            return PlanItemExecution.objects.create(student=student, plan_item=item, status=status.NOT_DONE, completed_at=now)
        execution.status = status.NOT_DONE
        execution.completed_at = now
        execution.save(update_fields=("status", "completed_at"))
        return execution

    if execution is None:
        raise ValidationError({"detail": "این فعالیت هنوز شروع نشده است."})
    if action == "pause":
        if execution.status != status.IN_PROGRESS:
            raise ValidationError({"detail": "فقط فعالیت در حال اجرا را می‌توان متوقف کرد."})
        execution.accumulated_seconds = execution.elapsed_seconds(now)
        execution.current_session_started_at = None
        execution.status = status.PAUSED
    elif action == "resume":
        if execution.status != status.PAUSED:
            raise ValidationError({"detail": "فقط فعالیت متوقف‌شده را می‌توان ادامه داد."})
        active_conflict(student, item)
        execution.current_session_started_at = now
        execution.status = status.IN_PROGRESS
    elif action == "finish":
        if execution.status not in (status.IN_PROGRESS, status.PAUSED):
            raise ValidationError({"detail": "این فعالیت قابل پایان دادن نیست."})
        if complete is None:
            raise ValidationError({"complete": "وضعیت انجام فعالیت را مشخص کنید."})
        execution.accumulated_seconds = execution.elapsed_seconds(now)
        execution.current_session_started_at = None
        execution.completed_at = now
        execution.status = status.COMPLETED if complete else status.PARTIAL
    else:
        raise ValueError(f"Unknown execution action: {action}")
    execution.save(update_fields=("status", "accumulated_seconds", "current_session_started_at", "completed_at"))
    return execution
