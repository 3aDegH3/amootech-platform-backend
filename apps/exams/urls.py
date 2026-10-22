from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CourseDetailView, CoursesView, EducationPeriodsView, GradesView, ExamViewSet, QuestionCountView, ProviderStatusView
from .study_sources import StudySourcesView
from .report_topics import ReportTopicResolveView, ReportTopicPreviewView, ReportTopicGroupsView, ReportScopedPreviewView

app_name = "exams"

router = DefaultRouter()
router.register("", ExamViewSet, basename="exam")

urlpatterns = [
    path("study-sources/topics/", ReportTopicGroupsView.as_view(), name="report-topic-groups"),
    path("study-sources/topics/preview/", ReportScopedPreviewView.as_view(), name="report-scoped-preview"),
    path("study-sources/", StudySourcesView.as_view(), name="study-sources"),
    path("study-sources/resolve/", ReportTopicResolveView.as_view(), name="study-source-resolve"),
    path("study-sources/preview/", ReportTopicPreviewView.as_view(), name="study-source-preview"),
    path("provider/status/", ProviderStatusView.as_view(), name="provider-status"),
    path("provider/question-count/", QuestionCountView.as_view(), name="question-count"),
    path("provider/education-periods/", EducationPeriodsView.as_view(), name="education-periods"),
    path("provider/grades/", GradesView.as_view(), name="grades"),
    path("provider/courses/", CoursesView.as_view(), name="courses"),
    path("provider/course-detail/", CourseDetailView.as_view(), name="course-detail"),
] + router.urls
