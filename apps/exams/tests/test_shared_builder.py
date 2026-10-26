"""Real local publication + DRF + PostgreSQL; remote creation is always mocked."""
from uuid import uuid4
from unittest.mock import patch
from django.test import override_settings
from rest_framework.test import APITestCase
from apps.accounts.models import User, CounselorProfile, StudentProfile
from apps.academics.models import Grade, Field
from apps.exams.models import ExamCreation, ExamAssignment
from .sprint_b_fixture import publish_recorded_catalog
from .catalog_fixture import link_student_fixture


@override_settings(QUIZ24_USER_ID='174037')
class SharedBuilderTests(APITestCase):
    def setUp(self):
        self.capture = publish_recorded_catalog()
        self.owner = User.objects.create_user('shared-owner', role='COUNSELOR')
        self.counselor = CounselorProfile.objects.create(user=self.owner)
        self.grades = [Grade.objects.create(number=n, name=name) for n,name in [(10,'دهم'),(11,'یازدهم'),(12,'دوازدهم')]]
        self.students = []
        for i,grade in enumerate((self.grades[0],self.grades[2])):
            student = StudentProfile.objects.create(user=User.objects.create_user(f'shared-{i}',role='STUDENT'),counselor=self.counselor,grade=grade,field=Field.objects.create(grade=grade,name='تجربی'))
            link_student_fixture(self,student); self.students.append(student)
        self.client.force_authenticate(self.owner)
        self.count = self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.question_count',return_value=self.capture['inventory']))
        self.create = self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.create_online_exam',return_value={'status':100,'result':123456}))
        self.enterContext(patch('httpx.Client.post',side_effect=AssertionError('Live HTTP forbidden')))
        self.payload = {'title':'آزمون مشترک','student_ids':[s.pk for s in self.students],'grade_external_id':11,'course_id':113,'question_type':1,'duration_minutes':20,'easy_count':1,'creation_key':str(uuid4())}

    def post(self, **change):
        return self.client.post('/api/v1/exams/automatic/',{**self.payload,**change},format='json')

    def test_group_cross_grade_eligibility_snapshot_assignments_and_replay(self):
        response = self.post()
        self.assertEqual(response.status_code,201,response.data)
        self.assertEqual(ExamAssignment.objects.filter(exam_id=response.data['id']).count(),2)
        receipt = ExamCreation.objects.get()
        self.assertEqual(receipt.confirmed_scope['selection']['student_ids'],self.payload['student_ids'])
        self.assertTrue(receipt.confirmed_scope['selection']['whole_course'])
        self.assertEqual(self.post().status_code,200)
        self.create.assert_called_once()

    def test_each_group_recipient_is_checked_before_inventory_and_creation(self):
        self.students[1].grade = Grade.objects.create(number=9,name='نهم')
        self.students[1].field = None; self.students[1].save()
        self.assertEqual(self.post().status_code,403)
        self.count.assert_not_called(); self.create.assert_not_called()

    def test_foreign_student_and_unlinked_recipient_are_rejected(self):
        self.students[1].counselor = None; self.students[1].save()
        self.assertEqual(self.post().status_code,403)
        self.students[1].counselor = self.counselor
        self.students[1].quiz24_sync_status = 'NOT_READY'; self.students[1].save()
        self.assertEqual(self.post().status_code,409)
        self.create.assert_not_called()

    def test_multi_course_and_forged_mixed_scope_never_reach_provider(self):
        self.assertEqual(self.post(courses=[{'course_id':113},{'course_id':3459}]).status_code,400)
        self.assertEqual(self.post(chapter_ids=[999999]).status_code,400)
        self.assertEqual(self.post(course_id=3459).status_code,400)
        self.count.assert_not_called();self.create.assert_not_called()

    def test_student_cannot_use_counselor_recipient_overrides(self):
        self.client.force_authenticate(self.students[0].user)
        self.assertEqual(self.post().status_code,400)
        self.create.assert_not_called()
