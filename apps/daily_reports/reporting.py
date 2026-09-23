"""Canonical bounded report aggregates built from the existing progress collector.

Academic time includes STUDY, TEST, REVIEW and EXAM; EVENT time is excluded.
Actual duration chooses one source per PlanItem: manual correction, then timer,
then the explicit same-as-plan shortcut. A report row never adds a second session.
Plan completion counts completed academic PlanItems only.
"""

from .progress import student_progress


def reporting_data(student, start_date, end_date, metric):
    data = student_progress(student, start_date=start_date, end_date=end_date,
                            include_items=True, include_analytics=True)
    academic = [item for item in data["planned_items"] if item["kind"] != "EVENT"]
    by_date = {}
    for item in academic:
        daily = by_date.setdefault(item["date"], {"total": 0, "completed": 0})
        daily["total"] += 1
        daily["completed"] += item["status"] == "COMPLETED"
    days = list(reversed(data["recent_days"]))
    completed = sum(day["completed"] for day in by_date.values())
    planned_blocks = len(academic)
    summary = {
        "planned_minutes": sum(day["planned_minutes"] for day in days),
        "actual_minutes": sum(day["actual_minutes"] for day in days),
        "planned_tests": sum(day["planned_tests"] for day in days),
        "actual_tests": sum(day["actual_tests"] for day in days),
        "completed_blocks": completed, "planned_blocks": planned_blocks,
        "completion_percent": round(completed * 100 / planned_blocks) if planned_blocks else None,
    }
    trend = []
    for day in days:
        counts = by_date.get(day["date"], {"total": 0, "completed": 0})
        if metric == "tests":
            value = day["actual_tests"]
        elif metric == "plan":
            value = day["completion_percent"] or 0
        else:
            value = day["actual_minutes"]
        trend.append({"date": day["date"], "value": value})
    return {
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "metric": metric,
        "summary": summary, "trend": trend, "days": days,
        "subjects": data["subjects"], "topic_repetition": data["topic_repetition"],
    }
