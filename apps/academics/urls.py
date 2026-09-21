from rest_framework.routers import DefaultRouter
from .views import GradeViewSet, FieldViewSet, SubjectViewSet, ChapterViewSet, TopicViewSet

router = DefaultRouter()
router.register("grades", GradeViewSet)
router.register("fields", FieldViewSet)
router.register("subjects", SubjectViewSet)
router.register("chapters", ChapterViewSet)
router.register("topics", TopicViewSet)
urlpatterns = router.urls
