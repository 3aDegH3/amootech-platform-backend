from django.urls import reverse
from rest_framework.test import APITestCase


class HealthTests(APITestCase):
    def test_health_endpoint(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
