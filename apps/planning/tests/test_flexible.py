from datetime import date
from unittest.mock import patch
from django.core.cache import cache
from rest_framework.test import APITestCase
from apps.accounts.models import User, CounselorProfile, StudentProfile
from apps.academics.models import Grade, Field, Subject, Chapter, Topic
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution
from apps.planning.titles import build_title


class FlexiblePlanningTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', role='COUNSELOR')
        self.counselor = CounselorProfile.objects.create(user=self.owner)
        self.user = User.objects.create_user('student')
        self.grade = Grade.objects.create(name='هفتم', number=7)
        self.field = Field.objects.create(grade=self.grade, name='عمومی')
        self.student = StudentProfile.objects.create(user=self.user, counselor=self.counselor, grade=self.grade, field=self.field)
        self.course = Subject.objects.create(field=self.field, name='فیزیک')
        self.chapter = Chapter.objects.create(subject=self.course, name='حرکت')
        self.topic = Topic.objects.create(chapter=self.chapter, name='سرعت')
        plan = Plan.objects.create(student=self.student, counselor=self.counselor, start_date=date(2026, 10, 1), end_date=date(2026, 10, 1))
        self.day = PlanDay.objects.create(plan=plan, date=plan.start_date)
        self.client.force_authenticate(self.owner)
        self.url = '/api/v1/planning/items/'

    def create(self, **kwargs):
        return self.client.post(self.url, {'plan_day': self.day.pk, 'kind': 'STUDY', 'planned_duration_minutes': 30, **kwargs}, format='json')

    def test_free_text_and_internal_levels(self):
        for selection, title in [({'title': 'آزاد'}, 'آزاد'), ({'subject': self.course.pk}, 'فیزیک'),
                                 ({'chapter': self.chapter.pk}, 'فیزیک — حرکت'), ({'topic': self.topic.pk}, 'فیزیک — سرعت')]:
            response = self.create(**selection)
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(response.data['title'], title)
        self.assertEqual(self.create().status_code, 400)

    def test_custom_reset_and_hierarchy(self):
        response = self.create(subject=self.course.pk, title='سفارشی')
        url = f"{self.url}{response.data['id']}/"
        response = self.client.patch(url, {'topic': self.topic.pk}, format='json')
        self.assertEqual(response.data['title'], 'سفارشی')
        response = self.client.patch(url, {'reset_generated_title': True}, format='json')
        self.assertEqual(response.data['title'], 'فیزیک — سرعت')
        self.assertFalse(response.data['title_is_custom'])
        other = Subject.objects.create(field=self.field, name='شیمی')
        self.assertEqual(self.client.patch(url, {'subject': other.pk}, format='json').status_code, 400)

    def test_ownership_and_execution_preserved(self):
        response = self.create(title='قدیمی')
        item = PlanItem.objects.get(pk=response.data['id'])
        execution = PlanItemExecution.objects.create(student=self.student, plan_item=item, status='COMPLETED')
        self.assertEqual(self.client.patch(f'{self.url}{item.pk}/', {'title': 'تغییر'}).status_code, 400)
        self.assertTrue(PlanItemExecution.objects.filter(pk=execution.pk, plan_item=item).exists())
        other = User.objects.create_user('other', role='COUNSELOR')
        CounselorProfile.objects.create(user=other)
        self.client.force_authenticate(other)
        self.assertEqual(self.create(title='مجاز نیست').status_code, 400)

    def test_titles_all_levels(self):
        self.assertEqual(build_title({'labels': {'grade': 'هفتم'}}), 'هفتم')
        for level in ('chapter', 'subject', 'object', 'topic'):
            self.assertEqual(build_title({'labels': {'course': 'فیزیک', level: 'سرعت'}}), 'فیزیک — سرعت')
        self.assertEqual(build_title({'labels': {'course': 'فیزیک', 'object': 'فیزیک'}}), 'فیزیک')

    def test_provider_ids_and_labels(self):
        def provider(method, **kwargs):
            return {'grades': {'result': [{'id': 700, 'name': 'هفتم'}]},
                    'courses': {'result': [{'id': 900, 'name': 'علوم', 'gradeId': 700}]},
                    'course_detail': {'result': [{'key': 900, 'title': 'علوم', 'type': 1, 'children': [
                        {'key': 901, 'title': 'فصل', 'type': 2, 'children': [
                            {'key': 902, 'title': 'مبحث', 'type': 3, 'children': [
                                {'key': 903, 'title': 'موضوع', 'type': 4, 'children': []}]}]}]}]}}[method]
        with patch('apps.academics.provider_catalog.catalog', side_effect=provider), patch('apps.exams.services.quiz24.Quiz24Service.create_exam') as mutation:
            for selection, expected in [({'grade': 700}, 'هفتم'), ({'course': 900}, 'علوم'),
                                        ({'course': 900, 'chapter': 901}, 'علوم — فصل'),
                                        ({'course': 900, 'object': 903}, 'علوم — موضوع')]:
                response = self.create(taxonomy={'provider': 'quiz24', **selection})
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data['title'], expected)
                self.assertEqual(response.data['taxonomy']['provider'], 'quiz24')
            self.assertEqual(self.create(taxonomy={'provider': 'quiz24', 'course': 900, 'chapter': 999}).status_code, 400)
            self.assertEqual(self.create(taxonomy={'provider': 'quiz24', 'course': 'علوم'}).status_code, 400)
            mutation.assert_not_called()

    def test_student_period_and_missing_grade(self):
        other = Grade.objects.create(name='اول', number=1)
        foreign = Field.objects.create(grade=other, name='ابتدایی')
        foreign_subject = Subject.objects.create(field=foreign, name='علوم ابتدایی')
        self.client.force_authenticate(self.user)
        response = self.client.get('/api/v1/academics/grades/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn(other.pk, [r['id'] for r in response.data['results']])
        self.assertEqual(self.client.get(f'/api/v1/academics/subjects/{foreign_subject.pk}/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/v1/academics/tree/?grade={other.pk}').data['subjects'], [])
        StudentProfile.objects.filter(pk=self.student.pk).update(grade=None, field=None)
        self.user.refresh_from_db()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get('/api/v1/academics/grades/').status_code, 403)
        with patch('apps.exams.services.quiz24.Quiz24Service.grades') as remote:
            self.assertEqual(self.client.get('/api/v1/exams/provider/grades/').status_code, 403)
            remote.assert_not_called()

    def test_provider_period_ids_and_upper_field_fail_closed(self):
        def provider(method, **kwargs):
            return {'result': ([{'id': 800, 'name': 'هفتم'}, {'id': 7, 'name': 'دهم'}]
                               if method == 'grades' else [
                                   {'id': 901, 'name': 'علوم', 'gradeId': 800},
                                   {'id': 902, 'name': 'فیزیک', 'gradeId': 7}])}
        self.client.force_authenticate(self.user)
        with patch('apps.academics.provider_catalog.catalog', side_effect=provider), patch('apps.exams.services.quiz24.Quiz24Service.course_detail') as detail:
            response = self.client.get('/api/v1/exams/provider/courses/')
            self.assertEqual([row['id'] for row in response.data['result']], [901])
            # An internal grade ID is not a provider grade identifier.
            response = self.client.post('/api/v1/exams/provider/course-detail/', {'grade_id': 7, 'course_id': 902})
            self.assertEqual(response.status_code, 403)
            detail.assert_not_called()
        upper = Grade.objects.create(name='دهم', number=10)
        upper_field = Field.objects.create(grade=upper, name='ریاضی')
        StudentProfile.objects.filter(pk=self.student.pk).update(grade=upper, field=upper_field)
        self.user.refresh_from_db()
        self.client.force_authenticate(self.user)
        with patch('apps.academics.provider_catalog.catalog', side_effect=provider):
            response = self.client.get('/api/v1/exams/provider/courses/')
            self.assertEqual(response.data['result'], [])
        other_field = Field.objects.create(grade=upper, name='تجربی')
        forbidden = Subject.objects.create(field=other_field, name='زیست')
        self.assertEqual(self.client.get(f'/api/v1/academics/subjects/{forbidden.pk}/').status_code, 404)

    def test_catalog_cache_and_unavailability(self):
        from apps.academics.provider_catalog import catalog
        from apps.exams.exceptions import Quiz24Error
        with patch('apps.exams.services.quiz24.Quiz24Service.grades', return_value={'result': []}) as remote:
            catalog('grades')
            catalog('grades')
            remote.assert_called_once()
        with patch('apps.academics.provider_catalog.catalog', side_effect=Quiz24Error(None, 'offline')):
            self.assertEqual(self.create(taxonomy={'provider': 'quiz24', 'grade': 10}).status_code, 400)
            self.assertEqual(self.create(title='آزاد').status_code, 201)

    def test_copy_preserves_taxonomy_and_title_state(self):
        from apps.planning.views import copy_item
        item = PlanItem.objects.create(plan_day=self.day, kind='STUDY', title='سفارشی',
                                       planned_duration_minutes=30, taxonomy={'provider': 'quiz24', 'course': 900, 'labels': {'course': 'علوم'}})
        copied = copy_item(item, self.day)
        self.assertNotEqual(copied.pk, item.pk)
        self.assertEqual(copied.taxonomy, item.taxonomy)
        self.assertEqual(copied.title_is_custom, item.title_is_custom)
