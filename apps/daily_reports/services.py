from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import StudentProfile
from apps.planning.models import PlanItem, PlanItemExecution
from .models import DailyReport, DailyReportItem

REPORT_ZONE = ZoneInfo("Asia/Tehran")
ACADEMIC_KINDS = {PlanItem.Kind.STUDY, PlanItem.Kind.TEST, PlanItem.Kind.REVIEW, PlanItem.Kind.EXAM}


def report_today():
    return timezone.localdate(timezone=REPORT_ZONE)


def check_editable(date):
    today = report_today()
    if date > today or date < today - timedelta(days=30):
        raise ValidationError({"date": "گزارش فقط برای امروز و ۳۰ روز گذشته قابل ویرایش است."})


@transaction.atomic
def open_report(user, date):
    student = StudentProfile.objects.select_for_update().get(user=user)
    report = DailyReport.objects.filter(student=student, date=date).first()
    if report and date != report_today():
        return report
    check_editable(date)
    report, _ = DailyReport.objects.get_or_create(student=student, date=date)
    planned = PlanItem.objects.filter(
        plan_day__plan__student=student, plan_day__date=date,
        plan_day__plan__status="PUBLISHED",
    ).order_by("plan_day__plan_id", "plan_day_id", "ordering", "pk")
    existing = set(report.items.filter(plan_item__isnull=False).values_list("plan_item_id", flat=True))
    DailyReportItem.objects.bulk_create([
        DailyReportItem(report=report, plan_item=item, ordering=index)
        for index, item in enumerate(planned) if item.pk not in existing
    ], ignore_conflicts=True)
    return report


def report_payload(report):
    now = timezone.now()
    rows = list(report.items.select_related(
        "plan_item__subject", "plan_item__chapter", "plan_item__topic",
        "subject", "chapter", "topic",
    ))
    plan_ids = [row.plan_item_id for row in rows if row.plan_item_id]
    executions = {
        record.plan_item_id: record for record in PlanItemExecution.objects.filter(
            student=report.student, plan_item_id__in=plan_ids,
        )
    }
    total_minutes = total_tests = completed = partial = not_done = uncompleted = academic_activities = planned_total = planned_minutes = planned_tests = 0
    by_subject = {}
    items = []
    for row in rows:
        planned = row.plan_item
        execution = executions.get(row.plan_item_id)
        kind = planned.kind if planned else row.kind
        subject = planned.subject if planned else row.subject
        chapter = planned.chapter if planned else row.chapter
        topic = planned.topic if planned else row.topic
        measured = round(execution.elapsed_seconds(now) / 60) if execution and execution.started_at else None
        if row.duration_source == DailyReportItem.DurationSource.MANUAL:
            actual_duration = row.actual_duration_minutes
            duration_source = "MANUAL"
        elif measured is not None:
            actual_duration = measured
            duration_source = "TIMER"
        elif row.duration_source == DailyReportItem.DurationSource.PLAN:
            actual_duration = row.actual_duration_minutes
            duration_source = "PLAN"
        else:
            actual_duration = None
            duration_source = None
        state = execution.status if execution else ("NOT_STARTED" if planned else "UNPLANNED")
        if planned and kind in ACADEMIC_KINDS:
            planned_total += 1
            planned_minutes += planned.planned_duration_minutes or 0
            planned_tests += planned.test_count or 0
            if state == PlanItemExecution.Status.COMPLETED:
                completed += 1
            elif state == PlanItemExecution.Status.PARTIAL:
                partial += 1
            elif state == PlanItemExecution.Status.NOT_DONE:
                not_done += 1
            else:
                uncompleted += 1
        if kind in ACADEMIC_KINDS:
            total_minutes += actual_duration or 0
            if not planned or state in (PlanItemExecution.Status.COMPLETED, PlanItemExecution.Status.PARTIAL) or actual_duration is not None or row.actual_test_count is not None:
                academic_activities += 1
            if subject and actual_duration is not None:
                by_subject[subject.name] = by_subject.get(subject.name, 0) + actual_duration
        total_tests += row.actual_test_count or 0
        items.append({
            "id": row.pk, "plan_item": row.plan_item_id, "kind": kind,
            "title": planned.title if planned else row.title,
            "resource": row.resource, "note": row.note,
            "start_time": (planned.start_time if planned else row.start_time).isoformat(timespec="minutes") if (planned.start_time if planned else row.start_time) else None,
            "end_time": (planned.end_time if planned else row.end_time).isoformat(timespec="minutes") if (planned.end_time if planned else row.end_time) else None,
            "subject": subject.pk if subject else None, "subject_name": subject.name if subject else None,
            "chapter": chapter.pk if chapter else None, "chapter_name": chapter.name if chapter else None,
            "topic": topic.pk if topic else None, "topic_name": topic.name if topic else None,
            "planned_duration_minutes": planned.planned_duration_minutes if planned else None,
            "planned_test_count": planned.test_count if planned else None,
            "actual_duration_minutes": actual_duration,
            "entered_actual_duration_minutes": row.actual_duration_minutes,
            "duration_source": duration_source,
            "actual_test_count": row.actual_test_count,
            "correct_count": row.correct_count, "wrong_count": row.wrong_count,
            "unanswered_count": row.unanswered_count,
            "execution_status": state,
        })
    return {
        "id": report.pk, "student": report.student_id, "date": report.date.isoformat(),
        "wake_time": report.wake_time.isoformat(timespec="minutes") if report.wake_time else None,
        "sleep_time": report.sleep_time.isoformat(timespec="minutes") if report.sleep_time else None,
        "mobile_minutes": report.mobile_minutes, "self_rating": report.self_rating,
        "note": report.note, "closed_at": report.closed_at.isoformat() if report.closed_at else None, "items": items,
        "summary": {
            "planned_minutes": planned_minutes, "total_actual_minutes": total_minutes,
            "planned_tests": planned_tests, "total_tests": total_tests,
            "completed_plan_items": completed, "partial_plan_items": partial,
            "not_done_plan_items": not_done,
            "uncompleted_plan_items": uncompleted, "planned_items": planned_total,
            "academic_activities": academic_activities,
            "by_subject": [{"name": name, "minutes": minutes} for name, minutes in by_subject.items()],
        },
    }
