from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from .models import User, StudentProfile, CounselorProfile
from .profile_serializers import StudentSerializer, CounselorSerializer


class AdminWrite(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and (
            request.method in ("GET", "HEAD", "OPTIONS") or request.user.role == User.Role.ADMIN
        )


class StudentViewSet(viewsets.ModelViewSet):
    serializer_class = StudentSerializer
    permission_classes = (AdminWrite,)
    queryset = StudentProfile.objects.select_related("user", "counselor__user", "grade", "field").order_by("pk")

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.role == User.Role.ADMIN:
            return queryset
        if user.role == User.Role.COUNSELOR:
            return queryset.filter(counselor__user=user)
        return queryset.filter(user=user)


class CounselorViewSet(viewsets.ModelViewSet):
    serializer_class = CounselorSerializer
    permission_classes = (AdminWrite,)
    queryset = CounselorProfile.objects.select_related("user").order_by("pk")

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.role == User.Role.ADMIN:
            return queryset
        if self.request.user.role == User.Role.COUNSELOR:
            return queryset.filter(user=self.request.user)
        return queryset.none()
