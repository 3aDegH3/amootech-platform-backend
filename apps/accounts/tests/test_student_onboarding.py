from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.accounts.models import CounselorProfile, StudentProfile
from apps.academics.models import Field, Grade

User = get_user_model()


class StudentOnboardingTests(APITestCase):
    def setUp(self):
        self.grade = Grade.objects.create(name="11")
        self.field = Field.objects.create(grade=self.grade, name="Math")
        self.other_grade = Grade.objects.create(name="12")
        self.other_field = Field.objects.create(grade=self.other_grade, name="Science")
        self.payload = {
            "username": "newstudent", "password": "SafePassword123!", "first_name": "New",
            "last_name": "Student", "grade": self.grade.pk, "field": self.field.pk,
            "school_name": "Example School",
        }

    def register(self, **changes):
        return self.client.post("/api/v1/auth/register/student/", {**self.payload, **changes})

    def test_registration_creates_student_and_profile_without_exposing_password(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(username="newstudent")
        profile = StudentProfile.objects.get(user=user)
        self.assertEqual(user.role, User.Role.STUDENT)
        self.assertTrue(user.check_password(self.payload["password"]))
        self.assertEqual(profile.grade, self.grade)
        self.assertEqual(profile.field, self.field)
        self.assertEqual(profile.school_name, "Example School")
        self.assertIsNone(profile.counselor)
        self.assertNotIn("password", response.data)
        self.assertNotIn("password", response.data["user"])

    def test_privileged_fields_cannot_be_injected(self):
        counselor = CounselorProfile.objects.create(user=User.objects.create_user("counselor", role=User.Role.COUNSELOR))
        for key, value in (("role", "ADMIN"), ("role", "COUNSELOR"), ("counselor", counselor.pk), ("is_staff", True)):
            with self.subTest(key=key, value=value):
                response = self.register(**{key: value})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(User.objects.filter(username="newstudent").exists())

    def test_invalid_field_or_duplicate_username_does_not_create_another_account(self):
        self.assertEqual(self.register(field=self.other_field.pk).status_code, 400)
        self.assertFalse(User.objects.filter(username="newstudent").exists())
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.register().status_code, 400)
        self.assertEqual(User.objects.filter(username="newstudent").count(), 1)
        self.assertEqual(StudentProfile.objects.filter(user__username="newstudent").count(), 1)

    def test_public_academic_options_are_read_only_and_active(self):
        Grade.objects.create(name="inactive", is_active=False)
        response = self.client.get("/api/v1/academics/grades/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.data["results"]], [self.grade.pk, self.other_grade.pk])
        self.assertEqual(self.client.post("/api/v1/academics/grades/", {"name": "13"}).status_code, 401)

    def test_self_profile_edit_is_scoped_and_counselor_is_protected(self):
        self.register()
        user = User.objects.get(username="newstudent")
        other = StudentProfile.objects.create(user=User.objects.create_user("other"))
        counselor = CounselorProfile.objects.create(user=User.objects.create_user("counselor", role=User.Role.COUNSELOR))
        path = "/api/v1/auth/profile/student/"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.client.force_authenticate(user)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("password", response.data)
        self.assertEqual(self.client.get(f"/api/v1/students/{other.pk}/").status_code, 404)
        self.assertEqual(self.client.patch(path, {"counselor": counselor.pk}).status_code, 400)
        self.assertIsNone(StudentProfile.objects.get(user=user).counselor)
        response = self.client.patch(path, {"first_name": "Updated", "grade": self.other_grade.pk, "field": self.other_field.pk, "school_name": "New School"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(User.objects.get(pk=user.pk).first_name, "Updated")
        self.assertEqual(StudentProfile.objects.get(user=user).school_name, "New School")
        self.assertEqual(self.client.patch(path, {"grade": self.grade.pk}).status_code, 400)

    def test_counselor_sees_assigned_student_summary(self):
        self.register()
        student = StudentProfile.objects.get(user__username="newstudent")
        counselor_user = User.objects.create_user("counselor", role=User.Role.COUNSELOR)
        student.counselor = CounselorProfile.objects.create(user=counselor_user)
        student.save()
        self.client.force_authenticate(counselor_user)
        response = self.client.get(f"/api/v1/students/{student.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["user"]["first_name"], "New")
        self.assertEqual(response.data["grade"], self.grade.pk)
        self.assertEqual(response.data["grade_name"], "11")
        self.assertEqual(response.data["field"], self.field.pk)
        self.assertEqual(response.data["field_name"], "Math")
        self.assertEqual(response.data["school_name"], "Example School")
        self.assertEqual(response.data["counselor_user"]["username"], "counselor")
        self.assertTrue(response.data["user"]["is_active"])
