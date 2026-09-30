from django.urls import path
from .views_internal import group_confirm, group_resolve, student_confirm
from .views_plan import plan_by_date, plan_excel, plan_pdf, plan_today, plan_tomorrow, plan_week

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
]
