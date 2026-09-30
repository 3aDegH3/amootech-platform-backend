from rest_framework.permissions import BasePermission

from apps.accounts.models import StudentProfile, User


def is_owner_counselor(user: User, student: StudentProfile) -> bool:
    return (
        user.role == User.Role.COUNSELOR
        and hasattr(user, "counselor_profile")
        and student.counselor_id == user.counselor_profile.pk
    )


class IsOwnerCounselorOrAdmin(BasePermission):
    """Check provided via view logic; this is a placeholder for Service auth."""

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated


class ServiceTokenPermission(BasePermission):
    """
    Service-to-Service: Authorization: Bearer <TELEGRAM_BOT_SERVICE_TOKEN>
    Only for /api/internal/v1/telegram/*.
    """

    def has_permission(self, request, view):
        from django.conf import settings

        expected = (getattr(settings, "TELEGRAM_BOT_SERVICE_TOKEN", "") or "").strip()
        if not expected:
            return False
        auth = request.headers.get("Authorization", "") if hasattr(request, "headers") else request.META.get("HTTP_AUTHORIZATION", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth.removeprefix("Bearer ").strip()
        import hmac

        return hmac.compare_digest(token, expected)

    def has_object_permission(self, request, view, obj):
        return self.has_permission(request, view)
