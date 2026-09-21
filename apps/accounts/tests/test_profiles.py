from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APITestCase
from apps.accounts.models import StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field

User = get_user_model()


class ProfileModelTests(TestCase):
    def test_role_validation_and_academic_consistency(self):
        student = User.objects.create_user("s", password="pass", role=User.Role.STUDENT)
        counselor = User.objects.create_user("c", password="pass", role=User.Role.COUNSELOR)
        with self.assertRaises(ValidationError):
            CounselorProfile.objects.create(user=student)
        with self.assertRaises(ValidationError):
            StudentProfile.objects.create(user=counselor)
        profile = CounselorProfile.objects.create(user=counselor)
        grade = Grade.objects.create(name="11")
        other = Grade.objects.create(name="12")
        field = Field.objects.create(grade=other, name="Math")
        with self.assertRaises(ValidationError):
            StudentProfile.objects.create(user=student, grade=grade, field=field)
        student_profile = StudentProfile.objects.create(user=student, counselor=profile, grade=other, field=field)
        self.assertEqual(student_profile.counselor, profile)


class ProfileAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", password="pass", role=User.Role.ADMIN)
        self.c1 = User.objects.create_user("c1", password="pass", role=User.Role.COUNSELOR)
        self.c2 = User.objects.create_user("c2", password="pass", role=User.Role.COUNSELOR)
        self.cp1 = CounselorProfile.objects.create(user=self.c1)
        self.cp2 = CounselorProfile.objects.create(user=self.c2)
        self.s1 = User.objects.create_user("s1", password="pass", role=User.Role.STUDENT)
        self.s2 = User.objects.create_user("s2", password="pass", role=User.Role.STUDENT)
        self.sp1 = StudentProfile.objects.create(user=self.s1, counselor=self.cp1)
        self.sp2 = StudentProfile.objects.create(user=self.s2, counselor=self.cp2)

    def test_admin_can_create_and_update_atomically(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post("/api/v1/counselors/", {"username": "newc", "password": "secretpass"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(User.objects.get(username="newc").role, User.Role.COUNSELOR)
        self.assertNotIn("password", response.data)
        response = self.client.post("/api/v1/students/", {"username": "news", "password": "secretpass", "counselor": response.data["id"]})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(StudentProfile.objects.get(user__username="news").counselor.user.username, "newc")
        self.assertEqual(self.client.patch(f"/api/v1/students/{response.data['id']}/", {"counselor": self.cp2.pk}).status_code, 200)
        before = User.objects.count()
        self.assertEqual(self.client.post("/api/v1/students/", {"username": "bad", "password": "secretpass", "counselor": 99999}).status_code, 400)
        self.assertEqual(User.objects.count(), before)

    def test_scoped_access(self):
        self.assertEqual(self.client.get("/api/v1/students/").status_code, 401)
        self.client.force_authenticate(self.c1)
        response = self.client.get("/api/v1/students/")
        self.assertEqual([x["id"] for x in response.data["results"]], [self.sp1.pk])
        self.assertEqual(self.client.get(f"/api/v1/students/{self.sp2.pk}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/v1/students/{self.sp1.pk}/", {"counselor": self.cp2.pk}).status_code, 403)
        self.client.force_authenticate(self.s1)
        self.assertEqual(self.client.get(f"/api/v1/students/{self.sp1.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/students/{self.sp2.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/counselors/{self.cp1.pk}/").status_code, 404)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/v1/students/").data["count"], 2)

    def test_invalid_grade_field_and_wrong_role_assignment(self):
        self.client.force_authenticate(self.admin)
        g1 = Grade.objects.create(name="11")
        g2 = Grade.objects.create(name="12")
        f = Field.objects.create(grade=g2, name="Math")
        response = self.client.patch(f"/api/v1/students/{self.sp1.pk}/", {"grade": g1.pk, "field": f.pk})
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(f"/api/v1/students/{self.sp1.pk}/", {"counselor": self.s1.pk})
        self.assertEqual(response.status_code, 400)
