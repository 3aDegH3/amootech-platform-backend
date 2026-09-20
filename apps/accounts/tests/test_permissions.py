from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase

from apps.accounts.models import User
from apps.accounts.permissions import IsAdmin, IsCounselor, IsStudent


class RolePermissionTests(SimpleTestCase):
    def test_each_permission_accepts_only_its_role(self):
        cases = (
            (IsAdmin, User.Role.ADMIN),
            (IsCounselor, User.Role.COUNSELOR),
            (IsStudent, User.Role.STUDENT),
        )
        for permission_class, allowed_role in cases:
            for role in User.Role.values:
                with self.subTest(permission=permission_class.__name__, role=role):
                    request = SimpleNamespace(
                        user=SimpleNamespace(is_authenticated=True, role=role)
                    )
                    self.assertEqual(
                        permission_class().has_permission(request, None),
                        role == allowed_role,
                    )

    def test_role_permissions_reject_anonymous_users(self):
        request = SimpleNamespace(user=AnonymousUser())
        for permission_class in (IsAdmin, IsCounselor, IsStudent):
            self.assertFalse(permission_class().has_permission(request, None))
