from unittest.mock import patch
from django.db import OperationalError
from django.urls import reverse
from rest_framework.test import APITestCase


class HealthTests(APITestCase):
    def test_health_endpoint(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_returns_unavailable_without_database_details(self):
        with patch("config.views.connection.cursor", side_effect=OperationalError("sensitive connection details")):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
