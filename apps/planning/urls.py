from rest_framework.routers import DefaultRouter

from .views import PlanDayViewSet, PlanItemViewSet, PlanViewSet, StudentFixedCommitmentViewSet, StudentSchoolSlotViewSet

router = DefaultRouter()
router.register("plans", PlanViewSet)
router.register("days", PlanDayViewSet)
router.register("items", PlanItemViewSet)
router.register("commitments", StudentFixedCommitmentViewSet, basename="commitments")
router.register("school-slots", StudentSchoolSlotViewSet, basename="school-slots")
urlpatterns = router.urls
