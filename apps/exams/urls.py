from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CourseDetailView, CoursesView, EducationPeriodsView, GradesView, ExamViewSet, QuestionCountView, ProviderStatusView

app_name = "exams"

router = DefaultRouter()
router.register("", ExamViewSet, basename="exam")

urlpatterns = [
    path("provider/status/", ProviderStatusView.as_view(), name="provider-status"),
    path("provider/question-count/", QuestionCountView.as_view(), name="question-count"),
    path("provider/education-periods/", EducationPeriodsView.as_view(), name="education-periods"),
    path("provider/grades/", GradesView.as_view(), name="grades"),
    path("provider/courses/", CoursesView.as_view(), name="courses"),
    path("provider/course-detail/", CourseDetailView.as_view(), name="course-detail"),
] + router.urls
