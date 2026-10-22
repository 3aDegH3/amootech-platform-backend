"""Recorded real Quiz24 catalogs/counts; provider mutations are always mocked."""
import copy
import json
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from django.core.cache import cache
from rest_framework.test import APITestCase
from apps.accounts.models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field
from apps.academics.provider_catalog import catalog
from apps.exams.models import Exam
from apps.exams.services.catalog import taxonomy_index


class AutomaticCatalogTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.recorded=json.loads((Path(__file__).parent/'fixtures/automatic_catalog.json').read_text())
        self.user=User.objects.create_user('real-catalog-student',role='STUDENT')
        owner=User.objects.create_user('real-catalog-owner',role='COUNSELOR')
        counselor=CounselorProfile.objects.create(user=owner)
        grade=Grade.objects.create(id=9012,name='دوازدهم',number=12)
        Grade.objects.create(id=9010,name='دهم',number=10)
        Grade.objects.create(id=9011,name='یازدهم',number=11)
        self.student=StudentProfile.objects.create(user=self.user,counselor=counselor,grade=grade,field=Field.objects.create(grade=grade,name='تجربی'))
        self.client.force_authenticate(self.user)
        self.grades=self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.grades',return_value=self.recorded['all_grades']))
        self.courses=self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.courses',return_value=self.recorded['all_courses']))
        self.detail=self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.course_detail',return_value=self.recorded['detail']))
        self.count=self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.question_count',return_value=self.recorded['provider_inventory']))
        self.create=self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.create_online_exam',return_value={'status':100,'result':987654321}))
        self.enterContext(patch('httpx.Client.post',side_effect=AssertionError('Unexpected live provider call')))

    def filters(self,level='object'):
        return copy.deepcopy(self.recorded['availability'][level]['filters'])

    def post_count(self,filters):
        return self.client.post('/api/v1/exams/provider/question-count/',filters,format='json')

    def test_shared_course_identifier_uses_grade_and_actual_field(self):
        grades=self.client.get('/api/v1/exams/provider/grades/')
        self.assertEqual(grades.status_code,200)
        self.assertEqual({r['id'] for r in grades.data['result']},{11,21,31})
        courses=self.client.get('/api/v1/exams/provider/courses/')
        self.assertEqual(courses.status_code,200)
        self.assertTrue(all(r['gradeId'] in (11,21,31) for r in courses.data['result']))
        shared=[r for r in self.recorded['all_courses']['result'] if r['courseId']==109]
        self.assertGreater(len(shared),1)
        response=self.post_count({**self.filters('course'),'course_id':109})
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(self.count.call_args.kwargs['grade_id'],11)
        self.assertEqual(self.count.call_args.kwargs['course_id'],109)
        for bad in ({'grade_id':10,'course_id':109},{'grade_id':9012},{'course_id':999999}):
            self.assertIn(self.post_count({**self.filters('course'),**bad}).status_code,(400,403))

    def test_each_intentional_depth_creates_owned_exam_with_exact_filters(self):
        for level in ('course','chapter','subject','object'):
            filters=self.filters(level)
            response=self.post_count(filters)
            self.assertEqual(response.status_code,200,response.data)
            payload={**filters,'grade_external_id':filters['grade_id'],'title':f'محدوده {level}','duration_minutes':10,'easy_count':1,'creation_key':str(uuid4())}
            payload.pop('grade_id')
            response=self.client.post('/api/v1/exams/automatic/',payload,format='json')
            self.assertEqual(response.status_code,201,response.data)
            exam=Exam.objects.get(pk=response.data['id'])
            self.assertEqual(exam.student,self.student)
            self.assertEqual(exam.provider_exam_code,'987654321')
            for key in ('grade_id','course_id','chapter_ids','subject_ids','object_ids'):
                self.assertEqual(self.create.call_args.kwargs.get(key,[]),filters.get(key,[]))
            again=self.client.post('/api/v1/exams/automatic/',payload,format='json')
            self.assertEqual(again.status_code,200,again.data)
            self.assertEqual(again.data['id'],exam.pk)
        self.assertEqual(self.create.call_count,4)
        own=self.client.get('/api/v1/exams/').data['results']
        self.assertEqual(len(own),4)

    def test_multiple_real_subtopics_keep_exact_ancestor_filters(self):
        index=taxonomy_index(self.recorded['detail'],113)
        paths=list(index['object'].values())
        parent=next(path['subject'] for path in paths if sum(p['subject']==path['subject'] for p in paths)>1)
        selected=[path for path in paths if path['subject']==parent][:2]
        filters={**self.filters('course'),'chapter_ids':sorted({p['chapter'] for p in selected}),'subject_ids':[parent],'object_ids':[p['object'] for p in selected]}
        response=self.post_count(filters)
        self.assertEqual(response.status_code,200,response.data)
        for key in ('chapter_ids','subject_ids','object_ids'):
            self.assertEqual(self.count.call_args.kwargs[key],filters[key])

    def test_numeric_string_ids_and_real_tree_string_ids(self):
        def strings(nodes):
            for row in nodes:
                for k in ('key','type','parentId'):
                    if row.get(k) is not None: row[k]=str(row[k])
                strings(row.get('children') or [])
        tree=copy.deepcopy(self.recorded['detail']);strings(tree['result']);self.detail.return_value=tree
        filters={key:([str(v) for v in value] if isinstance(value,list) else str(value) if isinstance(value,int) and not isinstance(value,bool) else value) for key,value in self.filters().items()}
        response=self.post_count(filters)
        self.assertEqual(response.status_code,200,response.data)

    def test_stale_cached_courses_and_tree_are_refreshed_before_rejecting_valid_scope(self):
        self.courses.return_value={'result':[]};catalog('courses')
        self.courses.return_value=self.recorded['all_courses']
        self.assertEqual(self.post_count(self.filters()).status_code,200)
        cache.clear()
        self.detail.return_value={'result':[]}
        catalog('course_detail',grade_id=11,course_id=113,consider_used_question=False)
        self.detail.return_value=self.recorded['detail']
        self.assertEqual(self.post_count(self.filters()).status_code,200)

    def test_invalid_ancestry_inventory_and_mixed_scope_do_not_create(self):
        filters=self.filters()
        index=taxonomy_index(self.recorded['detail'],113)
        other=next(k for k in index['chapter'] if k not in filters['chapter_ids'])
        for change in ({'object_ids':[999999]},{'chapter_ids':[other]},{'chapter_ids':filters['chapter_ids']+[other]}):
            response=self.post_count({**filters,**change})
            self.assertEqual(response.status_code,400,response.data)
        payload={**filters,'grade_external_id':11,'title':'بیش از موجودی','duration_minutes':10,'easy_count':self.recorded['availability']['object']['counts']['easy']+1}
        payload.pop('grade_id')
        self.assertEqual(self.client.post('/api/v1/exams/automatic/',payload,format='json').status_code,400)
        self.create.assert_not_called()
