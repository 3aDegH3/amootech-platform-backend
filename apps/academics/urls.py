from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import AcademicTreeView, GradeViewSet, FieldViewSet, SubjectViewSet, ChapterViewSet, TopicViewSet

router = DefaultRouter()
router.register("grades", GradeViewSet)
router.register("fields", FieldViewSet)
router.register("subjects", SubjectViewSet)
router.register("chapters", ChapterViewSet)
router.register("topics", TopicViewSet)
urlpatterns = [
    path("tree/", AcademicTreeView.as_view(), name="academic-tree"),
] + router.urls
