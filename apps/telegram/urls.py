from django.urls import path
from .views_execution import execution_today, execution_update
from .views_internal import group_confirm, group_resolve, student_confirm
from .views_plan import plan_by_date, plan_excel, plan_pdf, plan_today, plan_tomorrow, plan_week
from .views_report import report_extra_activity, report_finalize, report_patch, report_today_view, report_yesterday
from .views_subjects import telegram_subjects

urlpatterns = [
    path("internal/v1/telegram/connections/group/confirm/", group_confirm, name="telegram-group-confirm"),
    path("internal/v1/telegram/connections/group/resolve/", group_resolve, name="telegram-group-resolve"),
    path("internal/v1/telegram/connections/student/confirm/", student_confirm, name="telegram-student-confirm"),
    path("internal/v1/telegram/plan/today/", plan_today, name="telegram-plan-today"),
    path("internal/v1/telegram/plan/tomorrow/", plan_tomorrow, name="telegram-plan-tomorrow"),
    path("internal/v1/telegram/plan/day/", plan_by_date, name="telegram-plan-day"),
    path("internal/v1/telegram/plan/week/", plan_week, name="telegram-plan-week"),
    path("internal/v1/telegram/plan/current/pdf/", plan_pdf, name="telegram-plan-pdf"),
    path("internal/v1/telegram/plan/current/excel/", plan_excel, name="telegram-plan-excel"),
    path("internal/v1/telegram/execution/today/", execution_today, name="telegram-execution-today"),
    path("internal/v1/telegram/execution/plan-items/", execution_update, name="telegram-execution-update"),
    path("internal/v1/telegram/report/today/", report_today_view, name="telegram-report-today"),
    path("internal/v1/telegram/report/today/patch/", report_patch, name="telegram-report-patch"),
    path("internal/v1/telegram/report/today/finalize/", report_finalize, name="telegram-report-finalize"),
    path("internal/v1/telegram/report/yesterday/", report_yesterday, name="telegram-report-yesterday"),
    path("internal/v1/telegram/report/extra-activity/", report_extra_activity, name="telegram-report-extra"),
    path("internal/v1/telegram/report/subjects/", telegram_subjects, name="telegram-subjects"),
]
