from rest_framework.routers import DefaultRouter
from django.urls import path

from .views import CounselorStudentProgressView, CounselorStudentReportsView, DailyReportViewSet, StudentProgressView, StudentReportsView

router = DefaultRouter()
router.register("daily-reports", DailyReportViewSet, basename="daily-report")
urlpatterns = [
    path("student/progress/", StudentProgressView.as_view(), name="student-progress"),
    path("student/reports/", StudentReportsView.as_view(), name="student-reports"),
    path("counselor/students/<int:student_id>/progress/", CounselorStudentProgressView.as_view(), name="counselor-student-progress"),
    path("counselor/students/<int:student_id>/reports/", CounselorStudentReportsView.as_view(), name="counselor-student-reports"),
    *router.urls,
]
