from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import CurrentUserView, StudentRegistrationView, StudentSelfView

urlpatterns = [
    path("token/", TokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("me/", CurrentUserView.as_view(), name="current-user"),
    path("register/student/", StudentRegistrationView.as_view(), name="student-register"),
    path("profile/student/", StudentSelfView.as_view(), name="student-self-profile"),
]
