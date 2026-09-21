from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework.test import APITestCase
from apps.academics.models import Grade, Field, Subject, Chapter, Topic

User = get_user_model()


class AcademicAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", password="pass", role=User.Role.ADMIN)
        self.counselor = User.objects.create_user("c", password="pass", role=User.Role.COUNSELOR)
        self.student = User.objects.create_user("s", password="pass", role=User.Role.STUDENT)

    def test_crud_permissions_and_filters_for_each_level(self):
        parent = None
        cases = (("grades", "name", Grade), ("fields", "grade", Field), ("subjects", "field", Subject), ("chapters", "subject", Chapter), ("topics", "chapter", Topic))
        for plural, parent_key, model in cases:
            url = f"/api/v1/academics/{plural}/"
            data = {"name": plural}
            if parent is not None:
                data[parent_key] = parent.pk
            self.client.force_authenticate(self.admin)
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 201, response.data)
            obj = model.objects.get(pk=response.data["id"])
            self.assertEqual(self.client.patch(f"{url}{obj.pk}/", {"ordering": 2}).status_code, 200)
            self.assertEqual(self.client.get(url).data["count"], 1)
            if parent is not None:
                self.assertEqual(self.client.get(url, {parent_key: parent.pk}).data["count"], 1)
                self.assertEqual(self.client.get(url, {parent_key: "bad"}).status_code, 400)
            for user in (self.counselor, self.student):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(url).status_code, 200)
                self.assertEqual(self.client.post(url, data).status_code, 403)
                self.assertEqual(self.client.patch(f"{url}{obj.pk}/", {"name": "x"}).status_code, 403)
                self.assertEqual(self.client.delete(f"{url}{obj.pk}/").status_code, 403)
            self.client.force_authenticate(user=None)
            self.assertEqual(self.client.get(url).status_code, 200 if plural in ("grades", "fields") else 401)
            parent = obj

    def test_unique_sibling_names_and_delete(self):
        grade = Grade.objects.create(name="11")
        Field.objects.create(grade=grade, name="Math")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Field.objects.create(grade=grade, name="Math")
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f"/api/v1/academics/grades/{grade.pk}/").status_code, 409)
