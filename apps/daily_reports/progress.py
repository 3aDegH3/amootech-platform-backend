from datetime import timedelta

from django.utils import timezone

from apps.planning.models import PlanItem, PlanItemExecution
from .models import DailyReport, DailyReportItem
from .services import ACADEMIC_KINDS, report_today


def student_progress(student, days=7, include_items=False, start_date=None, end_date=None, include_analytics=False):
    today = report_today()
    start = start_date or today - timedelta(days=days - 1)
    end = end_date or today
    dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    summaries = {date: {
        "date": date.isoformat(), "planned_minutes": 0, "actual_minutes": 0,
        "planned_tests": 0, "actual_tests": 0, "completed": 0, "partial": 0,
        "remaining": 0, "not_done": 0, "planned_blocks": 0, "by_subject": {},
        "self_rating": None, "wake_time": None, "sleep_time": None,
        "mobile_minutes": None, "has_report": False,
    } for date in dates}

    plans = list(PlanItem.objects.filter(
        plan_day__plan__student=student, plan_day__plan__status="PUBLISHED",
        plan_day__date__range=(start, end),
    ).select_related("plan_day", "subject", "chapter", "topic"))
    plan_by_id = {item.pk: item for item in plans}
    reports = list(DailyReport.objects.filter(student=student, date__range=(start, end)))
    report_by_id = {report.pk: report for report in reports}
    report_rows = list(DailyReportItem.objects.filter(report_id__in=report_by_id).select_related("subject", "chapter", "topic"))
    report_row_by_plan = {row.plan_item_id: row for row in report_rows if row.plan_item_id in plan_by_id}
    executions = {execution.plan_item_id: execution for execution in PlanItemExecution.objects.filter(
        student=student, plan_item_id__in=plan_by_id,
    )}
    now = timezone.now()
    planned_details = []
    subject_stats = {}
    topic_stats = {}

    def subject_bucket(subject):
        return subject_stats.setdefault(subject.pk, {
            "id": subject.pk, "name": subject.name, "planned_minutes": 0, "actual_minutes": 0,
            "planned_tests": 0, "actual_tests": 0, "wrong_tests": 0, "wrong_entries": 0,
            "completed": 0, "incomplete": 0, "extra_minutes": 0, "extra_activities": 0,
            "completed_items": [], "incomplete_items": [], "extra_items": [],
        })

    def topic_bucket(topic, subject_id):
        return topic_stats.setdefault(topic.pk, {"id": topic.pk, "subject_id": subject_id, "name": topic.name, "planned": 0, "actual": 0,
                                                   "planned_minutes": 0, "actual_minutes": 0, "planned_tests": 0, "actual_tests": 0})

    def add_actual(summary, kind, subject, minutes, tests):
        if kind in ACADEMIC_KINDS:
            summary["actual_minutes"] += minutes or 0
            if subject and minutes is not None:
                name = subject.name
                summary["by_subject"][name] = summary["by_subject"].get(name, 0) + minutes
        summary["actual_tests"] += tests or 0

    for item in plans:
        summary = summaries[item.plan_day.date]
        if item.kind in ACADEMIC_KINDS:
            summary["planned_blocks"] += 1
            summary["planned_minutes"] += item.planned_duration_minutes or 0
        summary["planned_tests"] += item.test_count or 0
        execution = executions.get(item.pk)
        if item.kind in ACADEMIC_KINDS:
            if execution and execution.status == PlanItemExecution.Status.COMPLETED:
                summary["completed"] += 1
            elif execution and execution.status == PlanItemExecution.Status.PARTIAL:
                summary["partial"] += 1
            elif execution and execution.status == PlanItemExecution.Status.NOT_DONE:
                summary["not_done"] += 1
            else:
                summary["remaining"] += 1
        row = report_row_by_plan.get(item.pk)
        if row and row.duration_source == DailyReportItem.DurationSource.MANUAL:
            minutes = row.actual_duration_minutes
        elif execution and execution.started_at:
            minutes = round(execution.elapsed_seconds(now) / 60)
        elif row and row.duration_source == DailyReportItem.DurationSource.PLAN:
            minutes = row.actual_duration_minutes
        else:
            minutes = None
        add_actual(summary, item.kind, item.subject, minutes, row.actual_test_count if row else None)
        if include_analytics and item.kind in ACADEMIC_KINDS:
            if item.topic_id:
                topic = topic_bucket(item.topic, item.subject_id)
                topic["planned"] += 1
                topic["planned_minutes"] += item.planned_duration_minutes or 0
                topic["planned_tests"] += item.test_count or 0
                if (execution and execution.status in ("COMPLETED", "PARTIAL")) or (minutes or 0) > 0 or (row and (row.actual_test_count or 0) > 0):
                    topic["actual"] += 1
                topic["actual_minutes"] += minutes or 0
                topic["actual_tests"] += row.actual_test_count or 0 if row else 0
            if item.subject_id:
                bucket = subject_bucket(item.subject)
                bucket["planned_minutes"] += item.planned_duration_minutes or 0
                bucket["actual_minutes"] += minutes or 0
                bucket["planned_tests"] += item.test_count or 0
                bucket["actual_tests"] += row.actual_test_count or 0 if row else 0
                bucket["wrong_tests"] += row.wrong_count or 0 if row else 0
                bucket["wrong_entries"] += int(row is not None and row.wrong_count is not None)
                block = {"id": item.pk, "title": item.topic.name if item.topic else item.chapter.name if item.chapter else item.title or item.subject.name,
                         "planned_minutes": item.planned_duration_minutes, "actual_minutes": minutes,
                         "status": execution.status if execution else "NOT_STARTED"}
                if execution and execution.status == "COMPLETED":
                    bucket["completed"] += 1
                    bucket["completed_items"].append(block)
                else:
                    bucket["incomplete"] += 1
                    bucket["incomplete_items"].append(block)
        if include_items:
            details = (row.correct_count, row.wrong_count, row.unanswered_count) if row else (None, None, None)
            planned_details.append({
                "id": item.pk, "date": item.plan_day.date.isoformat(), "kind": item.kind,
                "title": item.title, "subject": item.subject.name if item.subject else None,
                "chapter": item.chapter.name if item.chapter else None,
                "topic": item.topic.name if item.topic else None,
                "planned_minutes": item.planned_duration_minutes,
                "actual_minutes": minutes,
                "planned_tests": item.test_count, "actual_tests": row.actual_test_count if row else None,
                "status": execution.status if execution else "NOT_STARTED",
                "correct": details[0], "wrong": details[1], "unanswered": details[2],
            })

    for row in report_rows:
        if row.plan_item_id:
            continue
        summary = summaries[report_by_id[row.report_id].date]
        add_actual(summary, row.kind, row.subject, row.actual_duration_minutes, row.actual_test_count)
        if include_analytics and row.kind in ACADEMIC_KINDS:
            if row.topic_id and ((row.actual_duration_minutes or 0) > 0 or (row.actual_test_count or 0) > 0):
                topic = topic_bucket(row.topic, row.subject_id)
                topic["actual"] += 1
                topic["actual_minutes"] += row.actual_duration_minutes or 0
                topic["actual_tests"] += row.actual_test_count or 0
            if row.subject_id:
                bucket = subject_bucket(row.subject)
                bucket["actual_minutes"] += row.actual_duration_minutes or 0
                bucket["actual_tests"] += row.actual_test_count or 0
                bucket["wrong_tests"] += row.wrong_count or 0
                bucket["wrong_entries"] += int(row.wrong_count is not None)
                bucket["extra_minutes"] += row.actual_duration_minutes or 0
                bucket["extra_activities"] += 1
                bucket["extra_items"].append({
                    "id": row.pk,
                    "title": row.topic.name if row.topic else row.chapter.name if row.chapter else row.title or row.subject.name,
                    "resource": row.resource, "actual_minutes": row.actual_duration_minutes,
                })

    for report in reports:
        summary = summaries[report.date]
        summary.update(
            has_report=True, self_rating=report.self_rating,
            wake_time=report.wake_time.isoformat(timespec="minutes") if report.wake_time else None,
            sleep_time=report.sleep_time.isoformat(timespec="minutes") if report.sleep_time else None,
            mobile_minutes=report.mobile_minutes,
        )
    for summary in summaries.values():
        summary["by_subject"] = [
            {"name": name, "minutes": minutes} for name, minutes in sorted(summary["by_subject"].items())
        ]
        summary["completion_percent"] = round(summary["completed"] * 100 / summary["planned_blocks"]) if summary["planned_blocks"] else None
    result = {"today": summaries.get(today), "recent_days": [summaries[date] for date in reversed(dates)]}
    if include_items:
        result["planned_items"] = sorted(planned_details, key=lambda item: (item["date"], item["id"]), reverse=True)
        subjects = {}
        for day in summaries.values():
            for subject in day["by_subject"]:
                subjects[subject["name"]] = subjects.get(subject["name"], 0) + subject["minutes"]
        result["subject_workload"] = [{"name": name, "minutes": minutes} for name, minutes in sorted(subjects.items())]
    if include_analytics:
        actual_total = sum(subject["actual_minutes"] for subject in subject_stats.values())
        result["subjects"] = [
            {**{key: value for key, value in subject.items() if key != "wrong_entries"},
             "wrong_tests": subject["wrong_tests"] if subject["wrong_entries"] else None,
             "distribution_percent": round(subject["actual_minutes"] * 100 / actual_total, 1) if actual_total else 0,
             "completion_percent": round(subject["completed"] * 100 / (subject["completed"] + subject["incomplete"])) if subject["completed"] + subject["incomplete"] else None}
            for subject in sorted(subject_stats.values(), key=lambda subject: subject["name"])
        ]
        result["topic_repetition"] = sorted(topic_stats.values(), key=lambda topic: topic["name"])
    return result
