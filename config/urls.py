from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from config.views import HealthView
from rest_framework.routers import DefaultRouter
from apps.accounts.profile_views import StudentViewSet, CounselorViewSet

profiles = DefaultRouter()
profiles.register("students", StudentViewSet)
profiles.register("counselors", CounselorViewSet)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    path("api/v1/", include(profiles.urls)),
    path("api/v1/academics/", include("apps.academics.urls")),
    path("api/v1/planning/", include("apps.planning.urls")),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/v1/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
