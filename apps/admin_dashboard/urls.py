from django.urls import path
from .views import bulk_assign, counselor_summary, dashboard_summary, recent_activity, telegram_summary

urlpatterns = [
    path("admin/dashboard/summary/", dashboard_summary, name="admin-dashboard-summary"),
    path("admin/telegram/summary/", telegram_summary, name="admin-telegram-summary"),
    path("admin/counselors/<int:counselor_id>/summary/", counselor_summary, name="admin-counselor-summary"),
    path("admin/students/bulk-assign/", bulk_assign, name="admin-bulk-assign"),
    path("admin/activity/", recent_activity, name="admin-activity"),
]
