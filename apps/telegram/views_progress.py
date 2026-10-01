from datetime import timedelta

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.daily_reports.progress import student_progress
from apps.daily_reports.services import report_today

from .permissions import ServiceTokenPermission
from .views_plan import _resolve_student


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def progress_today(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    data = student_progress(student, days=1)
    today_data = data["today"]
    # today_data from progress already has: completed/partial/remaining/not_done, planned_blocks, actual_minutes/tests
    has_data = today_data["planned_blocks"] > 0 or today_data["actual_minutes"] > 0 or today_data["actual_tests"] > 0
    # unresolved = remaining (no execution)
    # completion already in completion_percent (completed/planned_blocks)
    return Response({
        "date": today.isoformat(),
        "has_data": has_data,
        "study_minutes": today_data["actual_minutes"],
        "test_count": today_data["actual_tests"],
        "plan_completion_percentage": today_data["completion_percent"],
        "items": {
            "total": today_data["planned_blocks"],
            "completed": today_data["completed"],
            "partial": today_data["partial"],
            "not_completed": today_data["not_done"],
            "unresolved": today_data["remaining"],
        },
        "report_finalized": today_data["has_report"] and today_data["completion_percent"] is not None,  # has_report indicates daily report exists
        "has_report": today_data["has_report"],
    })


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def progress_7days(request):
    student, err = _resolve_student(request)
    if err:
        return err
    today = report_today()
    data = student_progress(student, days=7)
    days = data["recent_days"]  # 7 days reversed, today last? Actually recent_days is reversed(dates) so today first? Check: [summaries[date] for date in reversed(dates)] -> today first
    # Let's ensure chronological order for response: from 6 days ago to today
    days_chrono = list(reversed(days))  # now oldest first
    total_study = sum(d["actual_minutes"] for d in days)
    total_tests = sum(d["actual_tests"] for d in days)
    # Average completion: only days with planned_blocks > 0
    with_plan = [d for d in days if d["planned_blocks"] > 0 and d["completion_percent"] is not None]
    avg_completion = round(sum(d["completion_percent"] for d in with_plan) / len(with_plan)) if with_plan else None
    days_with_data = sum(1 for d in days if d["actual_minutes"] > 0 or d["actual_tests"] > 0 or d["planned_blocks"] > 0)
    per_day = []
    for d in days_chrono:
        per_day.append({
            "date": d["date"],
            "study_minutes": d["actual_minutes"],
            "test_count": d["actual_tests"],
            "plan_completion_percentage": d["completion_percent"],
            "finalized": d["has_report"],
        })
    return Response({
        "from_date": days_chrono[0]["date"] if days_chrono else None,
        "to_date": days_chrono[-1]["date"] if days_chrono else None,
        "summary": {
            "study_minutes": total_study,
            "test_count": total_tests,
            "average_plan_completion_percentage": avg_completion,
            "days_with_data": days_with_data,
        },
        "days": per_day,
    })
