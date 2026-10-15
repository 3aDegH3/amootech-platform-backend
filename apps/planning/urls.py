from rest_framework.routers import DefaultRouter

from .views import PlanDayViewSet, PlanItemViewSet, PlanViewSet, StudentFixedCommitmentViewSet, StudentFixedEventViewSet, StudentSchoolSlotViewSet

router = DefaultRouter()
router.register("plans", PlanViewSet)
router.register("days", PlanDayViewSet)
router.register("items", PlanItemViewSet)
router.register("commitments", StudentFixedCommitmentViewSet, basename="commitments")
router.register("school-slots", StudentSchoolSlotViewSet, basename="school-slots")
router.register("fixed-events", StudentFixedEventViewSet, basename="fixed-events")
urlpatterns = router.urls
