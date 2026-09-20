from rest_framework.permissions import BasePermission

from .models import User


class HasRole(BasePermission):
    allowed_roles: tuple[str, ...] = ()

    def has_permission(self, request, view):
        return bool(
            request.user.is_authenticated and request.user.role in self.allowed_roles
        )


class IsAdmin(HasRole):
    allowed_roles = (User.Role.ADMIN,)


class IsCounselor(HasRole):
    allowed_roles = (User.Role.COUNSELOR,)


class IsStudent(HasRole):
    allowed_roles = (User.Role.STUDENT,)
