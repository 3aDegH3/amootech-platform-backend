"""Exercise real DRF routes/serializers with recorded read-only Quiz24 responses.

The provider fixture was captured on 2026-10-21. Only the external service is
stubbed; student ownership, canonical metadata, catalog views and saves are real.
"""
import json
import os
from datetime import date
from pathlib import Path
from unittest.mock import patch
from django.core.cache import cache
from rest_framework.test import APITestCase
from apps.accounts.models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field
from apps.exams.services.quiz24 import Quiz24Error
from apps.planning.models import Plan, PlanDay, PlanItem


class ActivityCatalogContractTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/activity_provider.json').read_text())
        self.owner = User.objects.create_user('catalog-counselor', role='COUNSELOR')
        self.counselor = CounselorProfile.objects.create(user=self.owner)
        self.student_user = User.objects.create_user('catalog-student', role='STUDENT')
        self.grades = {n: Grade.objects.create(id=1000+n, number=n, name=name) for n, name in ((10,'دهم'),(11,'یازدهم'),(12,'دوازدهم'))}
        self.fields = {n: Field.objects.create(grade=g, name='تجربی') for n,g in self.grades.items()}
        self.student = StudentProfile.objects.create(user=self.student_user, counselor=self.counselor, grade=self.grades[10], field=self.fields[10])
        self.client.force_authenticate(self.owner)
        for method, value in [('grades',self.fixture['grades']),('courses',self.fixture['courses']),('course_detail',self.fixture['detail'])]:
            self.enterContext(patch(f'apps.exams.services.quiz24.Quiz24Service.{method}', return_value=value))
        self.enterContext(patch('httpx.Client.post', side_effect=AssertionError('Live provider calls prohibited')))

    def test_student_catalog_and_complete_creation_contract(self):
        profiles = []
        for n in (10,11,12):
            self.student.grade = self.grades[n]
            self.student.field = self.fields[n]
            self.student.save()
            profile = self.client.get(f'/api/v1/students/{self.student.pk}/')
            self.assertEqual(profile.status_code,200,profile.data)
            self.assertEqual(profile.data['grade'],1000+n)
            self.assertEqual(profile.data['field_name'],'تجربی')
            profiles.append(profile.json())
        canonical = self.client.get('/api/v1/academics/grades/')
        grades = self.client.get('/api/v1/exams/provider/grades/')
        courses = self.client.get('/api/v1/exams/provider/courses/')
        for response in (canonical, grades, courses): self.assertEqual(response.status_code,200,response.data)
        self.assertEqual({row['number'] for row in canonical.data['results']},{10,11,12})
        self.assertIn('دهم تجربی', [row['title'] for row in grades.data['result']])
        detail = self.client.post('/api/v1/exams/provider/course-detail/',{'grade_id':self.fixture['grade'],'course_id':self.fixture['course']},format='json')
        self.assertEqual(detail.status_code,200,detail.data)
        def deepest(nodes, path):
            for node in nodes:
                kind = {1:'course',2:'chapter',3:'subject',4:'object'}[node['type']]
                selected = {**path,kind:node['key']}
                if kind == 'object': return selected
                found = deepest(node.get('children') or [],selected)
                if found: return found
        selection = deepest(detail.data['result'],{'provider':'quiz24','grade':self.fixture['grade']})
        self.assertIn('object',selection)
        plan = Plan.objects.create(student=self.student,counselor=self.counselor,start_date=date(2026,10,21),end_date=date(2026,10,21))
        day = PlanDay.objects.create(plan=plan,date=plan.start_date)
        created = self.client.post('/api/v1/planning/items/',{'plan_day':day.pk,'kind':'STUDY','planned_duration_minutes':60,'taxonomy':selection},format='json')
        self.assertEqual(created.status_code,201,created.data)
        self.assertEqual(created.data['taxonomy']['grade'],self.fixture['grade'])
        self.assertEqual(PlanItem.objects.filter(plan_day=day).count(),1)
        fetched = self.client.get(f"/api/v1/planning/items/{created.data['id']}/")
        self.assertEqual(fetched.status_code,200)
        self.assertEqual(fetched.data['taxonomy'],created.data['taxonomy'])
        output = os.environ.get('ACTIVITY_CONTRACT_OUTPUT')
        if output:
            Path(output).write_text(json.dumps({'profiles':profiles,'canonical':canonical.json(),'grades':grades.json(),'courses':courses.json(),'detail':detail.json(),'created':created.json()},ensure_ascii=False,indent=2)+'\n')

    def test_failure_empty_and_missing_profile_are_distinct(self):
        with patch('apps.exams.services.quiz24.Quiz24Service.grades',side_effect=Quiz24Error(None,'catalog unavailable',api_status=503)):
            response=self.client.get('/api/v1/exams/provider/grades/')
            self.assertEqual(response.status_code,503)
        cache.clear()
        with patch('apps.exams.services.quiz24.Quiz24Service.grades',return_value={'status':100,'result':[]}):
            response=self.client.get('/api/v1/exams/provider/grades/')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.data['result'],[])
        self.assertEqual(self.client.get('/api/v1/students/999999/').status_code,404)
        orphan = User.objects.create_user('catalog-no-profile',role='STUDENT')
        self.client.force_authenticate(orphan)
        self.assertEqual(self.client.get('/api/v1/exams/provider/grades/').status_code,403)

    def test_new_study_requires_provider_grade_course_chapter_and_valid_parent(self):
        plan = Plan.objects.create(student=self.student, counselor=self.counselor,
                                   start_date=date(2026,10,21), end_date=date(2026,10,21))
        day = PlanDay.objects.create(plan=plan, date=plan.start_date)
        root = self.fixture['detail']['result'][0]
        chapter = root['children'][0]
        valid = {'provider':'quiz24','grade':self.fixture['grade'],
                 'course':self.fixture['course'],'chapter':chapter['key']}
        for taxonomy in ({}, {k:v for k,v in valid.items() if k != 'grade'},
                         {k:v for k,v in valid.items() if k != 'chapter'},
                         {**valid, 'chapter':2147483647}):
            response = self.client.post('/api/v1/planning/items/',
                {'plan_day':day.pk,'kind':'STUDY','title':'متن آزاد',
                 'planned_duration_minutes':60,'taxonomy':taxonomy},format='json')
            self.assertEqual(response.status_code,400,response.data)
        legacy = PlanItem.objects.create(plan_day=day,kind='STUDY',title='فعالیت قدیمی',planned_duration_minutes=60)
        response = self.client.patch(f'/api/v1/planning/items/{legacy.pk}/', {'title':'عنوان اصلاح شده'}, format='json')
        self.assertEqual(response.status_code,200,response.data)
