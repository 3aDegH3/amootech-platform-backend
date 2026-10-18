from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import CurrentUserView, StudentRegistrationView, StudentSelfView

from .mobile_views import MobileRequestView, MobileVerifyView, Quiz24SyncView

urlpatterns = [
    path("mobile/request/", MobileRequestView.as_view(), name="mobile-request"),
    path("mobile/verify/", MobileVerifyView.as_view(), name="mobile-verify"),
    path("quiz24/sync/", Quiz24SyncView.as_view(), name="student-quiz24-sync"),
    path("token/", TokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("me/", CurrentUserView.as_view(), name="current-user"),
    path("register/student/", StudentRegistrationView.as_view(), name="student-register"),
    path("profile/student/", StudentSelfView.as_view(), name="student-self-profile"),
]
