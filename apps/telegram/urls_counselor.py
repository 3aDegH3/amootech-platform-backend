from django.urls import path
from .views_counselor import group_disconnect, group_link, group_status

urlpatterns = [
    path("students/<int:student_id>/telegram/group-link/", group_link, name="telegram-group-link"),
    path("students/<int:student_id>/telegram/status/", group_status, name="telegram-status"),
    path("students/<int:student_id>/telegram/group/", group_disconnect, name="telegram-group-disconnect"),
]
