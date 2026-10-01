from django.urls import path
from .views_admin import (
    admin_audit,
    admin_ban,
    admin_disable,
    admin_enable,
    admin_group_lock,
    admin_group_unlock,
    admin_resend_plan,
    admin_resume,
    admin_suspend,
    admin_telegram_status,
    admin_unban,
)

urlpatterns = [
    path("admin/students/<int:student_id>/telegram/", admin_telegram_status, name="telegram-admin-status"),
    path("admin/students/<int:student_id>/telegram/enable/", admin_enable, name="telegram-admin-enable"),
    path("admin/students/<int:student_id>/telegram/disable/", admin_disable, name="telegram-admin-disable"),
    path("admin/students/<int:student_id>/telegram/suspend/", admin_suspend, name="telegram-admin-suspend"),
    path("admin/students/<int:student_id>/telegram/resume/", admin_resume, name="telegram-admin-resume"),
    path("admin/students/<int:student_id>/telegram/ban/", admin_ban, name="telegram-admin-ban"),
    path("admin/students/<int:student_id>/telegram/unban/", admin_unban, name="telegram-admin-unban"),
    path("admin/students/<int:student_id>/telegram/group/lock/", admin_group_lock, name="telegram-admin-lock"),
    path("admin/students/<int:student_id>/telegram/group/unlock/", admin_group_unlock, name="telegram-admin-unlock"),
    path("admin/students/<int:student_id>/telegram/resend-plan/", admin_resend_plan, name="telegram-admin-resend"),
    path("admin/students/<int:student_id>/telegram/audit/", admin_audit, name="telegram-admin-audit"),
]
