from unittest.mock import patch
from uuid import uuid4
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase
from apps.accounts.models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field
from apps.exams.models import Exam, ExamCreation

NAMES = ['اول','دوم','سوم','چهارم','پنجم','ششم','هفتم','هشتم','نهم','دهم','یازدهم','دوازدهم']


class QuestionBankTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.grades = {n: Grade.objects.create(name=name, number=n) for n,name in enumerate(NAMES,1)}
        self.user = User.objects.create_user('bank-student',role='STUDENT')
        self.student = StudentProfile.objects.create(user=self.user,grade=self.grades[12],field=Field.objects.create(grade=self.grades[12],name='ریاضی'))
        self.client.force_authenticate(self.user)
        self.catalog_grades = {'result':[{'gradeId':1000+n,'gradeName':name} for n,name in enumerate(NAMES,1)]}
        self.catalog_courses = {'result':[{'courseId':2000+n,'courseName':f'درس {name}','gradeId':1000+n,'fieldName':'ریاضی'} for n,name in enumerate(NAMES,1)] + [{'courseId':9000,'courseName':'زیست','gradeId':1012,'fieldName':'تجربی'}]}
        for method, response in (('grades',self.catalog_grades),('courses',self.catalog_courses)):
            patch(f'apps.exams.views.Quiz24Service.{method}',return_value=response).start()
        self.detail = patch('apps.exams.views.Quiz24Service.course_detail',side_effect=self.tree).start()
        self.count = patch('apps.exams.views.Quiz24Service.question_count',return_value={'result':{'easyCount':10,'mediumCount':0,'hardCount':0,'veryHardCount':0}}).start()
        self.create = patch('apps.exams.views.Quiz24Service.create_online_exam',return_value={'result':123456,'status':100}).start()
        patch('httpx.Client.post',side_effect=AssertionError('Unexpected live provider call')).start()
        self.addCleanup(patch.stopall)

    def tree(self,grade_id,course_id,consider_used_question=False):
        n=course_id-2000
        def node(key,kind,parent,children=[]):
            return {'key':key,'title':'عنوان','type':kind,'parentId':parent,'children':children}
        return {'result':[node(course_id,1,None,[
            node(3000+n,2,course_id,[node(4000+n,3,3000+n,[node(5000+n,4,4000+n)])]),
            node(6000+n,2,course_id,[node(7000+n,3,6000+n,[node(8000+n,4,7000+n)])]),
        ])]}

    def filters(self,n=12,**changes):
        return {'grade_id':1000+n,'course_id':2000+n,'question_type':1,**changes}

    def request_count(self,n=12,**changes):
        return self.client.post('/api/v1/exams/provider/question-count/',self.filters(n,**changes),format='json')

    def request_create(self,n=12,**changes):
        filters=self.filters(n)
        return self.client.post('/api/v1/exams/automatic/',{**filters,'grade_external_id':filters.pop('grade_id'),'title':'تمرین','duration_minutes':20,'easy_count':2,'creation_key':str(uuid4()),**changes},format='json')

    def profile(self,n):
        self.student.grade=self.grades[n]
        self.student.field=Field.objects.get_or_create(grade=self.grades[n],name='ریاضی')[0] if n>=10 else None
        self.student.save()
        self.user.refresh_from_db();self.client.force_authenticate(self.user)

    def test_primary_lower_upper_period_discovery_and_counts(self):
        for current,allowed in ((3,range(1,7)),(8,range(7,10)),(12,range(10,13))):
            self.profile(current)
            response=self.client.get('/api/v1/exams/provider/grades/')
            self.assertEqual(response.status_code,200,response.data)
            self.assertEqual({row['gradeId'] for row in response.data['result']},{1000+n for n in allowed})
            courses=self.client.get('/api/v1/exams/provider/courses/').data['result']
            self.assertEqual({row['courseId'] for row in courses},{2000+n for n in allowed})
            for n in range(1,13):
                self.assertEqual(self.request_count(n).status_code,200 if n in allowed else 403)

    def test_current_period_cross_grade_create_own_practice_and_no_duplicates(self):
        for n in (10,11,12):
            response=self.request_create(n)
            self.assertEqual(response.status_code,201,response.data)
            exam=Exam.objects.get(pk=response.data['id'])
            self.assertEqual(exam.student_id,self.student.pk)
            self.assertTrue(exam.is_personal_practice)
            self.assertEqual(exam.status,'PUBLISHED')
        key=str(uuid4())
        first=self.request_create(10,creation_key=key)
        second=self.request_create(10,creation_key=key)
        self.assertEqual(first.data['id'],second.data['id'])
        self.assertEqual(self.create.call_count,4)

    def test_mismatched_grade_field_and_forged_ids_reject_before_count_or_create(self):
        for changes in ({'course_id':2009},{'course_id':2011},{'course_id':9000},{'course_id':999999}):
            self.assertIn(self.request_count(**changes).status_code,(400,403))
            self.assertIn(self.request_create(**changes).status_code,(400,403))
        for changes in ({'chapter_ids':[9999]}, {'chapter_ids':[3012],'subject_ids':[7012]},
                        {'chapter_ids':[3012],'subject_ids':[4012],'object_ids':[8012]},
                        {'chapter_ids':[3012,3012]}, {'chapter_ids':[3011]}):
            self.assertEqual(self.request_count(**changes).status_code,400)
            self.assertEqual(self.request_create(**changes).status_code,400)
        for n in (1,9):
            self.assertEqual(self.request_create(n).status_code,403)
        self.count.assert_not_called();self.create.assert_not_called()
        self.assertFalse(ExamCreation.objects.exists())

    def test_multi_topic_valid_path_and_unavailable_inventory(self):
        selection={'chapter_ids':[3012,6012],'subject_ids':[4012,7012],'object_ids':[5012,8012]}
        self.assertEqual(self.request_count(**selection).status_code,200)
        response=self.request_create(**selection)
        self.assertEqual(response.status_code,201,response.data)
        self.assertEqual(self.create.call_args.kwargs['object_ids'],[5012,8012])
        self.count.return_value={'result':{'easyCount':0,'mediumCount':0,'hardCount':0,'veryHardCount':0}}
        self.assertEqual(self.request_create().status_code,400)
        self.assertEqual(self.create.call_count,1)

    def test_missing_invalid_grade_and_unconfirmed_field_fail_closed(self):
        for values in ({'grade':None,'field':None},{'grade':self.grades[12],'field':None}):
            for name,value in values.items():setattr(self.student,name,value)
            self.student.save();self.user.refresh_from_db();self.client.force_authenticate(self.user)
            self.assertIn(self.request_count().status_code,(400,403))
            self.assertIn(self.request_create().status_code,(400,403))
        self.student.grade=Grade.objects.create(name='نامعتبر');self.student.field=None;self.student.save()
        self.user.refresh_from_db();self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get('/api/v1/exams/provider/grades/').status_code,403)
        self.count.assert_not_called();self.create.assert_not_called()

    def test_discovery_stale_hierarchy_and_counselor_catalog(self):
        for n in (1,9):
            response=self.client.post('/api/v1/exams/provider/course-detail/',self.filters(n),format='json')
            self.assertEqual(response.status_code,403)
        self.detail.assert_not_called()
        self.detail.return_value={'result':[]};self.detail.side_effect=None
        self.assertEqual(self.request_count(chapter_ids=[3012]).status_code,400)
        self.assertEqual(self.request_create(chapter_ids=[3012]).status_code,400)
        counselor_user=User.objects.create_user('counselor-bank',role='COUNSELOR')
        CounselorProfile.objects.create(user=counselor_user)
        self.client.force_authenticate(counselor_user)
        self.assertEqual(len(self.client.get('/api/v1/exams/provider/grades/').data['result']),12)
        self.assertEqual(len(self.client.get('/api/v1/exams/provider/courses/').data['result']),13)

    def test_student_owner_forgery_rejected_before_any_provider_creation(self):
        for key,value in (('student',self.student.pk),('student_id',None),('counselor_id',1)):
            self.assertEqual(self.request_create(**{key:value}).status_code,400)
        self.create.assert_not_called()

    def test_profile_change_during_availability_denies_final_provider_mutation(self):
        def inventory(**kwargs):
            StudentProfile.objects.filter(pk=self.student.pk).update(grade=self.grades[9], field=None)
            return {'result':{'easyCount':10,'mediumCount':0,'hardCount':0,'veryHardCount':0}}
        self.count.side_effect=inventory
        self.assertEqual(self.request_create().status_code,403)
        self.create.assert_not_called()

    def test_participation_keeps_existing_verified_identity_guard(self):
        response=self.request_create()
        self.assertEqual(response.status_code,201,response.data)
        with patch('apps.exams.views.Quiz24Service.get_participation_url') as participation:
            self.assertEqual(self.client.post(f"/api/v1/exams/{response.data['id']}/start/").status_code,409)
            participation.assert_not_called()

    def test_missing_profile_invalid_canonical_number_and_unmapped_field_fail_safely(self):
        missing = User.objects.create_user('missing-bank-profile', role='STUDENT')
        self.client.force_authenticate(missing)
        self.assertEqual(self.client.get('/api/v1/exams/provider/grades/').status_code,403)
        self.assertEqual(self.request_create().status_code,400)
        self.client.force_authenticate(self.user)
        Grade.objects.filter(pk=self.grades[12].pk).update(number=99)
        self.user.refresh_from_db();self.client.force_authenticate(self.user)
        self.assertEqual(self.request_count().status_code,403)
        Grade.objects.filter(pk=self.grades[12].pk).update(number=12)
        for row in self.catalog_courses['result']:
            row.pop('fieldName',None)
        cache.clear();self.user.refresh_from_db();self.client.force_authenticate(self.user)
        self.assertEqual(self.request_create().status_code,403)
        self.count.assert_not_called();self.create.assert_not_called()

    def test_malformed_or_different_course_tree_never_broadens_scope(self):
        self.detail.side_effect=None
        for response in ({'result':{}},self.tree(1009,2009)):
            cache.clear();self.detail.return_value=response
            self.assertEqual(self.request_count(chapter_ids=[3012]).status_code,400)
            self.assertEqual(self.request_create(chapter_ids=[3012]).status_code,400)
            self.assertEqual(self.client.post('/api/v1/exams/provider/course-detail/', self.filters(),format='json').status_code,400)
        self.count.assert_not_called();self.create.assert_not_called()

    def test_malformed_catalog_identifiers_are_not_exposed_as_student_courses(self):
        self.catalog_courses['result'].append({'courseName':'نامعتبر','gradeId':1001})
        cache.clear()
        response=self.client.get('/api/v1/exams/provider/courses/')
        self.assertEqual(response.status_code,200,response.data)
        self.assertTrue(all('courseId' in row for row in response.data['result']))

    def test_ambiguous_grade_identifier_cannot_authorize_another_period(self):
        self.catalog_grades['result'].append({'gradeId': 1012, 'gradeName': 'نهم'})
        cache.clear()
        response = self.client.get('/api/v1/exams/provider/grades/')
        self.assertNotIn(1012, [row['gradeId'] for row in response.data['result']])
        self.assertEqual(self.request_count().status_code, 403)
        self.assertEqual(self.request_create().status_code, 403)
        self.count.assert_not_called()
        self.create.assert_not_called()

    def test_normalized_tree_with_conflicting_ancestry_is_rejected(self):
        self.detail.side_effect = None
        self.detail.return_value = {'result': {'chapters': [{
            'id': 3012, 'title': 'فصل', 'subjects': [{
                'id': 4012, 'title': 'مبحث', 'chapterId': 3011, 'objects': [],
            }],
        }]}}
        self.assertEqual(self.request_count(chapter_ids=[3012], subject_ids=[4012]).status_code, 400)
        self.assertEqual(self.request_create(chapter_ids=[3012], subject_ids=[4012]).status_code, 400)
        self.count.assert_not_called()
        self.create.assert_not_called()

    def test_malformed_inventory_returns_provider_error_without_creation(self):
        for response in (None, [], 'invalid', [{'easyCount': True}]):
            self.count.return_value = response
            self.assertEqual(self.request_count().status_code, 502)
            self.assertEqual(self.request_create().status_code, 502)
        self.create.assert_not_called()
