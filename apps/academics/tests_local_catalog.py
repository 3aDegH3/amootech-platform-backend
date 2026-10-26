from copy import deepcopy
from datetime import date, time, timedelta
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase
from apps.accounts.models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field, ProviderCatalogNode, ProviderCatalogState, ProviderCatalogRevision
from apps.academics.local_catalog import sync_catalog, read_catalog
from apps.exams.exceptions import Quiz24Error
from apps.planning.models import Plan, PlanDay, StudentFixedEvent

# Shape and IDs recorded in the read-only Quiz24 activity fixture.
import json
from pathlib import Path
RECORDED = json.loads((Path(__file__).parents[1] / 'planning/tests/fixtures/activity_provider.json').read_text())


def provider_fixture():
    data = deepcopy(RECORDED)
    data['grades']['result'] = [row for row in data['grades']['result'] if row['id'] == data['grade']]
    data['courses']['result'] = [row for row in data['courses']['result'] if row['gradeId'] == data['grade'] and row['courseId'] == data['course']]
    row = data['grades']['result'][0]
    data['education_periods'] = {'status':100,'result':[{'id':row['educationPeriodId'],'title':row['educationPeriodTitle']}]}
    return data


class LocalCatalogTests(APITransactionTestCase):
    def setUp(self):
        self.data = provider_fixture()
        self.service = self.enterContext(patch('apps.academics.local_catalog.Quiz24Service'))
        for method in ('education_periods','grades','courses'):
            getattr(self.service.return_value, method).side_effect = lambda method=method: deepcopy(self.data[method])
        self.service.return_value.course_detail.side_effect = lambda **kwargs: deepcopy(self.data['detail'])
        self.admin = User.objects.create_user('admin',role='ADMIN')

    def test_full_import_offline_browsing_idempotency_and_monthly_due(self):
        state = sync_catalog()
        self.assertGreater(ProviderCatalogNode.objects.filter(kind='chapter').count(),0)
        original = ProviderCatalogNode.objects.count()
        sync_catalog()
        self.assertEqual(ProviderCatalogNode.objects.count(),original)
        self.assertEqual(ProviderCatalogRevision.objects.count(),2)
        sync_catalog(due_only=True)
        self.assertEqual(ProviderCatalogRevision.objects.count(),2)
        with patch("apps.academics.local_catalog.timezone.now", return_value=timezone.now()+timedelta(days=32)):
            sync_catalog(due_only=True)
        self.assertEqual(ProviderCatalogRevision.objects.count(),3)
        self.service.return_value.grades.side_effect = AssertionError('Network forbidden')
        self.assertEqual(read_catalog('grades')['result'][0]['id'], self.data['grade'])
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get('/api/v1/exams/provider/grades/').status_code,200)

    def test_partial_failure_preserves_published_revision(self):
        state = sync_catalog()
        revision = state.revision_id
        self.service.return_value.course_detail.side_effect = Quiz24Error(None,'offline',http_status=400)
        with self.assertRaises(Quiz24Error): sync_catalog()
        self.assertEqual(ProviderCatalogState.objects.get(pk=1).revision_id,revision)
        self.assertEqual(read_catalog('grades')['result'][0]['id'],self.data['grade'])
        self.assertEqual(ProviderCatalogState.objects.get(pk=1).status,'FAILED')

    def test_monthly_due_uses_tehran_month_at_utc_boundary(self):
        from datetime import datetime, timezone as datetime_timezone
        with patch('apps.academics.local_catalog.timezone.now', return_value=datetime(2026,10,31,19,tzinfo=datetime_timezone.utc)):
            sync_catalog()
        # Still October in UTC; November has begun in Tehran.
        with patch('apps.academics.local_catalog.timezone.now', return_value=datetime(2026,10,31,21,tzinfo=datetime_timezone.utc)):
            sync_catalog(due_only=True)
        self.assertEqual(ProviderCatalogRevision.objects.count(),2)
        with patch('apps.academics.local_catalog.timezone.now', return_value=datetime(2026,11,1,1,tzinfo=datetime_timezone.utc)):
            sync_catalog(due_only=True)
        self.assertEqual(ProviderCatalogRevision.objects.count(),2)

    def test_invalid_parent_and_duplicate_rejected(self):
        self.data['detail']['result'][0]['children'][0]['parentId'] = 999999
        with self.assertRaises(Exception): sync_catalog()
        self.assertFalse(ProviderCatalogRevision.objects.exists())
        self.data = provider_fixture()
        self.data['grades']['result'] *= 2
        with self.assertRaises(ValueError): sync_catalog()

    def test_removed_nodes_retained_only_after_explicit_review(self):
        sync_catalog()
        count=ProviderCatalogNode.objects.count()
        self.data['detail']['result'][0]['children'] = []
        with self.assertRaises(ValueError): sync_catalog()
        sync_catalog(allow_removals=True)
        self.assertEqual(ProviderCatalogNode.objects.count(),count)
        self.assertTrue(ProviderCatalogNode.objects.filter(active=False).exists())

    def test_admin_manual_enqueue_and_permission(self):
        student=User.objects.create_user('student',role='STUDENT')
        self.client.force_authenticate(student)
        self.assertEqual(self.client.post('/api/v1/academics/provider-sync/').status_code,403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post('/api/v1/academics/provider-sync/').status_code,202)
        self.assertTrue(ProviderCatalogState.objects.get(pk=1).requested)
        sync_catalog(due_only=True)
        self.assertFalse(ProviderCatalogState.objects.get(pk=1).requested)

    def test_watch_worker_consumes_admin_queue(self):
        from django.core.management import call_command
        import io
        sync_catalog()
        ProviderCatalogState.objects.filter(pk=1).update(requested=True)
        # Keep test connection lifecycle separate from the worker's lifecycle.
        with patch('apps.academics.management.commands.sync_quiz24_catalog.close_old_connections'), patch('apps.academics.management.commands.sync_quiz24_catalog.time.sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                call_command('sync_quiz24_catalog',watch=True,stdout=io.StringIO())
        self.assertFalse(ProviderCatalogState.objects.get(pk=1).requested)
        self.assertEqual(ProviderCatalogRevision.objects.count(),2)

    def test_requested_sync_publishes_changed_labels_for_offline_consumers(self):
        sync_catalog()
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post('/api/v1/academics/provider-sync/').status_code, 202)
        row = self.data['courses']['result'][0]
        row['courseName'] += ' (ویرایش تأمین‌کننده)'
        sync_catalog(due_only=True)
        self.service.return_value.courses.side_effect = AssertionError('Offline browsing must not fetch')
        response = self.client.get('/api/v1/exams/provider/courses/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['result'][0]['courseName'], row['courseName'])
        node = ProviderCatalogNode.objects.get(kind='course', provider_id=self.data['course'])
        self.assertEqual(node.title, row['courseName'])

    def test_admin_retry_is_consumed_during_automatic_failure_backoff(self):
        from django.core.management import call_command
        import io
        state = ProviderCatalogState.objects.create(pk=1, requested=True)
        command = 'apps.academics.management.commands.sync_quiz24_catalog.'
        with patch(command + 'sync_catalog', side_effect=[RuntimeError('offline'), state]) as sync, \
             patch(command + 'close_old_connections'), \
             patch(command + 'time.sleep', side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                call_command('sync_quiz24_catalog', watch=True, stdout=io.StringIO())
        self.assertEqual(sync.call_count, 2)

    def test_existing_envelopes_expose_verified_availability_and_sibling_order(self):
        sync_catalog()
        grade = read_catalog('grades')['result'][0]
        self.assertTrue(grade['active'])
        self.assertEqual(grade['providerId'], self.data['grade'])
        self.assertEqual(grade['level'], 'grade')
        from apps.academics.provider_catalog import field
        expected = {row['key']: field(row, 'order', 'ordering') for row in self.data['detail']['result'][0]['children']}
        for node in ProviderCatalogNode.objects.filter(kind='chapter'):
            self.assertEqual(node.ordering, expected[node.provider_id])

    def test_retry_bounded_and_pagination_fails_closed(self):
        self.service.return_value.grades.side_effect=[Quiz24Error(None,'offline'),self.data['grades']]
        with patch('apps.academics.local_catalog.time.sleep') as sleep:
            sync_catalog()
        sleep.assert_called_once_with(1)
        self.service.return_value.grades.side_effect=lambda:deepcopy(self.data['grades'])
        self.data['courses']['total']=999
        with self.assertRaises(ValueError):sync_catalog()

    def test_orphan_provider_courses_are_visible_in_quarantine_never_selectable(self):
        # All five rows observed in the official read-only response on 2026-10-26.
        invalid = [(4929,122,'روان شناسی سوم'), (4913,206,'آیین نامه راهنمایی و رانندگی'),
                   (4914,206,'پیام های اسمانی هفتم،نسخه آزمایشی'),
                   (4919,206,'هوش و استعداد معلمی'), (4920,206,'تعلیم و تربیت اسلامی')]
        self.data['courses']['result'].extend({'courseId':identifier,'courseName':name,'gradeId':grade}
                                             for identifier,grade,name in invalid)
        state=sync_catalog()
        self.assertEqual(state.revision.counts['quarantined_courses'],5)
        self.assertEqual(len(read_catalog('courses')['result']),1)
        self.assertFalse(ProviderCatalogNode.objects.filter(provider_id__in=[row[0] for row in invalid],kind='course',active=True).exists())
        self.assertEqual(state.revision.payload['quarantine'][0]['reason'],'MISSING_PROVIDER_GRADE')
        self.assertFalse(ProviderCatalogNode.objects.exclude(kind='period').filter(parent__isnull=True).exists())
        self.client.force_authenticate(self.admin)
        with patch('apps.exams.views.Quiz24Service.question_count') as inventory:
            for identifier,grade,_ in invalid:
                response = self.client.post('/api/v1/exams/provider/question-count/',
                    {'grade_id':grade,'course_id':identifier,'question_type':1},format='json')
                self.assertEqual(response.status_code,400)
            inventory.assert_not_called()

    def test_finish_all_levels_and_student_scope(self):
        sync_catalog()
        owner=User.objects.create_user('counselor',role='COUNSELOR')
        counselor=CounselorProfile.objects.create(user=owner)
        grade=Grade.objects.create(number=10,name='دهم')
        field=Field.objects.create(grade=grade,name='تجربی')
        student=StudentProfile.objects.create(user=User.objects.create_user('learner',role='STUDENT'),counselor=counselor,grade=grade,field=field)
        plan=Plan.objects.create(student=student,counselor=counselor,start_date=date(2026,10,24),end_date=date(2026,10,30))
        day=PlanDay.objects.create(plan=plan,date=plan.start_date)
        self.client.force_authenticate(owner)
        scope={'provider':'quiz24','grade':self.data['grade']}
        response=self.client.post('/api/v1/planning/items/',{'plan_day':day.pk,'kind':'STUDY','taxonomy':scope,'planned_duration_minutes':30},format='json')
        self.assertEqual(response.status_code,201,response.data)
        scope['course']=self.data['course']
        def paths(nodes, path):
            for row in nodes:
                kind={1:'course',2:'chapter',3:'subject',4:'object'}[row['type']]
                current={**path,kind:row['key']}
                yield current
                yield from paths(row['children'],current)
        for selected in paths(self.data['detail']['result'],scope):
            response=self.client.post('/api/v1/planning/items/',{'plan_day':day.pk,'kind':'STUDY','taxonomy':selected,'planned_duration_minutes':30,'title':'مرور دستی'},format='json')
            self.assertEqual(response.status_code,201,response.data)
            self.assertTrue(response.data['title_is_custom'])
        other=StudentProfile.objects.create(user=User.objects.create_user('foreign',role='STUDENT'))
        self.assertEqual(self.client.get(f'/api/v1/exams/provider/grades/?student={other.pk}').status_code,403)
        self.assertEqual(self.client.get(f'/api/v1/exams/provider/grades/?student={student.pk}').status_code,200)


class SchoolMarkerTests(APITestCase):
    def setUp(self):
        self.owner=User.objects.create_user('counselor',role='COUNSELOR')
        counselor=CounselorProfile.objects.create(user=self.owner)
        self.student=StudentProfile.objects.create(user=User.objects.create_user('student',role='STUDENT'),counselor=counselor)
        self.plan=Plan.objects.create(student=self.student,counselor=counselor,start_date=date(2026,10,24),end_date=date(2026,11,6))
        self.client.force_authenticate(self.owner)

    def test_one_time_marker_cannot_be_hidden_by_validity_bounds(self):
        response = self.client.post('/api/v1/planning/fixed-events/', {
            'student': self.student.pk, 'title': 'امتحان', 'kind': 'EXAM',
            'date': '2026-10-24', 'valid_from': '2026-10-25',
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(StudentFixedEvent.objects.exists())

    def test_all_weekdays_exact_occurrences_edit_stop_publish(self):
        for wd in range(7):
            result=self.client.post('/api/v1/planning/fixed-events/',{'student':self.student.pk,'title':str(wd),'kind':'QUIZ','weekday':wd},format='json')
            self.assertEqual(result.status_code,201,result.data)
            repeat=self.client.post('/api/v1/planning/fixed-events/',{'student':self.student.pk,'title':str(wd),'kind':'QUIZ','weekday':wd},format='json')
            self.assertEqual(repeat.data['id'],result.data['id'])
        path=f'/api/v1/planning/plans/{self.plan.pk}/'
        first=self.client.get(path).data['school_events']
        self.assertEqual(len(first),14)
        self.assertEqual(first,self.client.get(path).data['school_events'])
        for occurrence in first:
            self.assertEqual(date.fromisoformat(occurrence['date']).weekday(),int(occurrence['title']))
        one=self.client.post('/api/v1/planning/fixed-events/',{'student':self.student.pk,'title':'امتحان شنبه','kind':'EXAM','date':'2026-10-24'},format='json')
        self.assertEqual(one.status_code,201,one.data)
        self.assertEqual(len(self.client.get(path).data['school_events']),15)
        self.client.patch(f"/api/v1/planning/fixed-events/{one.data['id']}/",{'date':'2026-10-25'},format='json')
        self.assertEqual(self.client.get(path).data['school_events'][1]['date'],'2026-10-25')
        self.client.delete(f"/api/v1/planning/fixed-events/{one.data['id']}/")
        event=StudentFixedEvent.objects.filter(weekday=5).get()
        self.client.patch(f'/api/v1/planning/fixed-events/{event.pk}/',{'active':False},format='json')
        self.assertEqual(len(self.client.get(path).data['school_events']),12)
        self.client.force_authenticate(self.student.user)
        self.assertEqual(self.client.get(path).status_code,404)
        self.plan.status='PUBLISHED';self.plan.published_at=timezone.now();self.plan.save()
        self.assertEqual(len(self.client.get(path).data['school_events']),12)
        self.assertEqual(self.client.post('/api/v1/planning/fixed-events/',{'student':self.student.pk,'title':'bad','date':'2026-10-24'},format='json').status_code,400)


from django.test import TransactionTestCase
from django.db import connection, connections, transaction
from unittest import skipUnless
import threading


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL advisory lock contract')
class CatalogConcurrencyTests(TransactionTestCase):
    def test_second_worker_cannot_fetch_or_publish_under_existing_lock(self):
        ProviderCatalogState.objects.create(pk=1)
        locked = threading.Event()
        release = threading.Event()
        def hold():
            try:
                with transaction.atomic():
                    with connections['default'].cursor() as cursor:
                        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [240024])
                    locked.set()
                    release.wait(10)
            finally:
                connections['default'].close()
        worker=threading.Thread(target=hold)
        worker.start()
        self.assertTrue(locked.wait(5))
        try:
            with patch('apps.academics.local_catalog.Quiz24Service') as service:
                with self.assertRaises(RuntimeError):sync_catalog()
                service.assert_not_called()
            self.assertFalse(ProviderCatalogRevision.objects.exists())
        finally:
            release.set();worker.join(5)
