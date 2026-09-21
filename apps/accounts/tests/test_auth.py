from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken
from django.utils import timezone
from datetime import timedelta


class UserModelTests(APITestCase):
    def test_user_creation_hashes_password_and_defaults_to_student(self):
        user = get_user_model().objects.create_user("student", password="test-password")
        self.assertTrue(user.check_password("test-password"))
        self.assertEqual(user.role, get_user_model().Role.STUDENT)

    def test_role_values_are_explicit(self):
        self.assertEqual(
            set(get_user_model().Role.values), {"ADMIN", "COUNSELOR", "STUDENT"}
        )

    def test_invalid_role_fails_model_validation(self):
        user = get_user_model()(username="invalid-role", role="INVALID")
        with self.assertRaises(ValidationError):
            user.full_clean()

    def test_database_rejects_invalid_role(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create(username="invalid-db-role", role="INVALID")

    def test_superuser_defaults_to_admin_role(self):
        user = get_user_model().objects.create_superuser("admin", password="password")
        self.assertEqual(user.role, get_user_model().Role.ADMIN)


class AuthenticationTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "student", email="student@example.com", password="test-password"
        )

    def test_token_pair_and_refresh(self):
        response = self.client.post(
            reverse("token-obtain-pair"),
            {"username": "student", "password": "test-password"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        refresh = self.client.post(
            reverse("token-refresh"), {"refresh": response.data["refresh"]}
        )
        self.assertEqual(refresh.status_code, status.HTTP_200_OK)
        self.assertIn("access", refresh.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.data['access']}")
        me = self.client.get(reverse("current-user"))
        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertEqual(me.data["username"], "student")

    def test_expired_access_rejected_but_refresh_still_works(self):
        pair = self.client.post(
            reverse("token-obtain-pair"),
            {"username": "student", "password": "test-password"},
        ).data
        expired = AccessToken(pair["access"])
        expired.set_exp(from_time=timezone.now() - timedelta(hours=1), lifetime=timedelta(minutes=1))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(expired)}")
        self.assertEqual(self.client.get(reverse("current-user")).status_code, status.HTTP_401_UNAUTHORIZED)
        refreshed = self.client.post(reverse("token-refresh"), {"refresh": pair["refresh"]})
        self.assertEqual(refreshed.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}")
        self.assertEqual(self.client.get(reverse("current-user")).status_code, status.HTTP_200_OK)

    def test_authenticated_current_user(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("current-user"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], "student")
        self.assertEqual(response.data["role"], "STUDENT")
        self.assertNotIn("password", response.data)

    def test_anonymous_current_user_is_rejected(self):
        response = self.client.get(reverse("current-user"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
