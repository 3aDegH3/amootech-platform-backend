from rest_framework.routers import DefaultRouter

from .views import PlanDayViewSet, PlanItemViewSet, PlanViewSet, StudentFixedCommitmentViewSet

router = DefaultRouter()
router.register("plans", PlanViewSet)
router.register("days", PlanDayViewSet)
router.register("items", PlanItemViewSet)
router.register("commitments", StudentFixedCommitmentViewSet, basename="commitments")
urlpatterns = router.urls
