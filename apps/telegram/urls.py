from django.urls import path
from .views_internal import group_confirm, group_resolve, student_confirm

urlpatterns = [
    path("internal/v1/telegram/connections/group/confirm/", group_confirm, name="telegram-group-confirm"),
    path("internal/v1/telegram/connections/group/resolve/", group_resolve, name="telegram-group-resolve"),
    path("internal/v1/telegram/connections/student/confirm/", student_confirm, name="telegram-student-confirm"),
]
