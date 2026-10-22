from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from apps.accounts.models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field, Subject, Chapter, Topic
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.planning.models import Plan, PlanDay, PlanItem, PlanItemExecution

URL = '/api/v1/exams/study-sources/'
TODAY = date(2026, 10, 21)


class StudySourceTests(APITestCase):
    def setUp(self):
        self.counselor_user = User.objects.create_user('source-counselor', role='COUNSELOR')
        self.counselor = CounselorProfile.objects.create(user=self.counselor_user)
        self.other_counselor_user = User.objects.create_user('source-other-counselor', role='COUNSELOR')
        self.other_counselor = CounselorProfile.objects.create(user=self.other_counselor_user)
        self.user = User.objects.create_user('source-student', role='STUDENT')
        self.grade = Grade.objects.create(name='دوازدهم', number=12)
        self.field = Field.objects.create(grade=self.grade, name='ریاضی')
        self.subject = Subject.objects.create(field=self.field, name='حسابان')
        self.chapter = Chapter.objects.create(subject=self.subject, name='مشتق')
        self.topic = Topic.objects.create(chapter=self.chapter, name='نقطه بحرانی')
        self.student = StudentProfile.objects.create(user=self.user, counselor=self.counselor, grade=self.grade, field=self.field)
        self.other_user = User.objects.create_user('source-other', role='STUDENT')
        self.other = StudentProfile.objects.create(user=self.other_user, counselor=self.other_counselor)
        self.plan = Plan.objects.create(student=self.student, counselor=self.counselor,
            start_date=TODAY-timedelta(days=400), end_date=TODAY, status='PUBLISHED',
            published_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.client.force_authenticate(self.user)
        self.today_patch = patch('apps.exams.study_sources.report_today', return_value=TODAY)
        self.today_patch.start()
        # Any network/catalog access is an error, including read-only cached adapters.
        patch('httpx.Client.post', side_effect=AssertionError('No provider calls allowed')).start()
        patch('apps.academics.provider_catalog.catalog', side_effect=AssertionError('No catalog lookup allowed')).start()
        self.addCleanup(patch.stopall)

    def activity(self, day=TODAY, status='COMPLETED', **fields):
        plan_day, _ = PlanDay.objects.get_or_create(plan=self.plan, date=day)
        item = PlanItem.objects.create(plan_day=plan_day, kind='STUDY', planned_duration_minutes=20,
                                      title='مطالعه', title_is_custom=False, **fields)
        report, _ = DailyReport.objects.get_or_create(student=self.student, date=day)
        row = DailyReportItem.objects.create(report=report, plan_item=item, reported_status=status)
        return item, row

    def get(self, **params):
        return self.client.get(URL, params)

    def test_only_explicit_declarations_are_eligible(self):
        completed, row = self.activity(subject=self.subject)
        self.activity(status='PARTIAL')
        self.activity(status='NOT_DONE')
        unreported, unset = self.activity(status=None)
        DailyReportItem.objects.filter(pk=unset.pk).update(actual_duration_minutes=30, duration_source='MANUAL')
        PlanItemExecution.objects.create(student=self.student, plan_item=unreported, status='COMPLETED', accumulated_seconds=1200)
        PlanItem.objects.create(plan_day=completed.plan_day, kind='STUDY', title='بدون گزارش', planned_duration_minutes=30)
        response = self.get()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(response.data['summary']['by_status'], {'COMPLETED': 1, 'PARTIAL': 1})
        self.assertEqual({r['reported_status'] for r in response.data['results']}, {'COMPLETED', 'PARTIAL'})
        self.assertEqual(response.data['results'][0]['source_ref'], f'daily-report-item:{row.pk}')

    def test_internal_chapter_and_deep_topic_use_actual_hierarchy(self):
        for fields, level in (({'chapter': self.chapter}, 'chapter'), ({'topic': self.topic}, 'topic')):
            _, row = self.activity(**fields)
            result = next(r for r in self.get().data['results'] if r['report_item_id'] == row.pk)
            self.assertEqual(result['state'], 'SUGGESTED')
            self.assertTrue(result['academic_context_usable'])
            self.assertFalse(result['needs_taxonomy_selection'])
            self.assertTrue(result['requires_topic_confirmation'])
            self.assertEqual(result['suggested_selection']['deepest_level'], level)
            self.assertEqual(result['suggested_selection']['hierarchy']['course']['id'], self.subject.pk)
            self.assertNotIn('studied_topics', result)

    def test_custom_title_is_preserved_and_not_parsed(self):
        item, row = self.activity(chapter=self.chapter)
        PlanItem.objects.filter(pk=item.pk).update(title='مشتق و انتگرال فصل 999999', title_is_custom=True)
        result = self.get().data['results'][0]
        self.assertEqual(result['title'], 'مشتق و انتگرال فصل 999999')
        self.assertEqual(result['state'], 'NEEDS_CONFIRMATION')
        self.assertIn('CUSTOM_TITLE_MAY_DESCRIBE_ADDITIONAL_TOPICS', result['reasons'])
        self.assertNotIn('topic', result['suggested_selection']['hierarchy'])

    def test_free_text_and_unplanned_explicit_reports(self):
        self.activity()
        report = DailyReport.objects.get(student=self.student, date=TODAY)
        extra = DailyReportItem.objects.create(report=report, kind='STUDY', title='آزاد', subject=self.subject,
            actual_duration_minutes=10, duration_source='MANUAL', reported_status='PARTIAL')
        self.assertEqual(self.get().data['count'], 2)
        rows = self.get().data['results']
        free = next(r for r in rows if r['plan_item_id'])
        self.assertEqual(free['state'], 'NEEDS_SELECTION')
        self.assertIsNone(free['suggested_selection'])
        result = next(r for r in rows if r['report_item_id'] == extra.pk)
        self.assertIsNone(result['plan_item_id'])
        self.assertTrue(result['academic_context_usable'])
        extra.delete()
        self.assertEqual(self.get().data['count'], 1)

    def test_unplanned_without_metadata_is_eligible_but_needs_selection(self):
        report = DailyReport.objects.create(student=self.student, date=TODAY)
        # Legacy/imported free-text rows can lack the currently-required subject FK.
        DailyReportItem.objects.bulk_create([DailyReportItem(report=report, kind='REVIEW', title='مرور آزاد', reported_status='COMPLETED')])
        result = self.get().data['results'][0]
        self.assertEqual(result['state'], 'NEEDS_SELECTION')
        self.assertEqual(result['title'], 'مرور آزاد')

    def test_provider_subject_object_or_sparse_context_is_unverified_without_calls(self):
        for metadata in (
            {'provider':'quiz24','grade':700,'course':900,'chapter':901,'subject':902,'object':903,
             'labels':{'grade':'دوازدهم','course':'فیزیک','chapter':'حرکت','subject':'سرعت','object':'سرعت متوسط'}},
            {'provider':'quiz24','course':900,'subject':902,'labels':{'course':'فیزیک','subject':'سرعت'}},
        ):
            _, row = self.activity(taxonomy=metadata)
            result = next(r for r in self.get().data['results'] if r['report_item_id']==row.pk)
            self.assertEqual(result['state'], 'NEEDS_CONFIRMATION')
            self.assertEqual(result['taxonomy_snapshot']['provider_context'], metadata)
            self.assertIsNone(result['suggested_selection'])
            self.assertFalse(result['academic_context_usable'])
            self.assertIn('PROVIDER_CONTEXT_REQUIRES_REVALIDATION', result['reasons'])

    def test_invalid_taxonomy_and_mixed_namespaces_do_not_grant_scope(self):
        for metadata in ({'provider':'unknown'}, {'provider':'quiz24','object':123}, ['malformed'], []):
            _, row = self.activity(taxonomy=metadata)
            result = next(r for r in self.get().data['results'] if r['report_item_id']==row.pk)
            self.assertIsNone(result['suggested_selection'])
            self.assertEqual(result['state'], 'NEEDS_CONFIRMATION')
        self.activity(subject=self.subject, taxonomy={'provider':'quiz24','course':900})
        self.assertIsNone(self.get().data['results'][-1]['suggested_selection'])

    def test_inconsistent_internal_ancestry_and_inactive_nodes(self):
        other_subject = Subject.objects.create(field=self.field, name='دیگر')
        item, _ = self.activity(chapter=self.chapter)
        PlanItem.objects.filter(pk=item.pk).update(subject=other_subject)
        self.assertIn('INVALID_INTERNAL_HIERARCHY', self.get().data['results'][0]['reasons'])
        PlanItem.objects.filter(pk=item.pk).update(subject=self.subject)
        Chapter.objects.filter(pk=self.chapter.pk).update(is_active=False)
        self.assertFalse(self.get().data['results'][0]['academic_context_usable'])

    def test_period_and_field_policy_also_applies_to_counselor_extraction(self):
        grade9 = Grade.objects.create(name='نهم', number=9)
        field9 = Field.objects.create(grade=grade9, name='عمومی')
        science = Subject.objects.create(field=field9, name='علوم')
        bio_field = Field.objects.create(grade=self.grade, name='تجربی')
        biology = Subject.objects.create(field=bio_field, name='زیست')
        grade10 = Grade.objects.create(name='دهم', number=10)
        math10 = Field.objects.create(grade=grade10, name='ریاضی')
        math = Subject.objects.create(field=math10, name='ریاضی دهم')
        for subject in (science, biology, math):
            self.activity(subject=subject)
        for user, params in ((self.user, {}), (self.counselor_user, {'student_id': self.student.pk})):
            self.client.force_authenticate(user)
            rows = self.get(**params).data['results']
            self.assertEqual([r['academic_context_usable'] for r in rows], [False, False, True])

    def test_missing_grade_or_field_keeps_report_but_marks_context_unusable(self):
        self.activity(subject=self.subject)
        for changes in ({'grade':None,'field':None}, {'grade':self.grade,'field':None}):
            StudentProfile.objects.filter(pk=self.student.pk).update(**changes)
            response = self.get()
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.data['results'][0]['academic_context_usable'])

    def test_report_edits_are_reflected_without_changing_provenance(self):
        _, row = self.activity()
        original = self.get().data['results'][0]['source_ref']
        DailyReportItem.objects.filter(pk=row.pk).update(reported_status='PARTIAL')
        self.assertEqual(self.get().data['results'][0]['reported_status'], 'PARTIAL')
        self.assertEqual(self.get().data['results'][0]['source_ref'], original)
        DailyReportItem.objects.filter(pk=row.pk).update(reported_status='NOT_DONE')
        self.assertEqual(self.get().data['count'], 0)

    def test_invalid_foreign_or_wrong_date_plan_links_are_excluded(self):
        item, row = self.activity()
        PlanDay.objects.filter(pk=item.plan_day_id).update(date=TODAY-timedelta(days=1))
        self.assertEqual(self.get().data['count'], 0)
        PlanDay.objects.filter(pk=item.plan_day_id).update(date=TODAY)
        Plan.objects.filter(pk=self.plan.pk).update(student=self.other)
        self.assertEqual(self.get().data['count'], 0)
        Plan.objects.filter(pk=self.plan.pk).update(student=self.student, status='DRAFT')
        self.assertEqual(self.get().data['count'], 0)
        with self.assertRaises(ProtectedError):
            item.delete()

    def test_historical_ranges_and_repeated_topics_preserve_occurrences(self):
        self.activity(topic=self.topic)
        _, old = self.activity(day=TODAY-timedelta(days=120), topic=self.topic)
        _, yesterday = self.activity(day=TODAY-timedelta(days=1), topic=self.topic)
        self.assertEqual(self.get().data['count'], 1)
        response = self.get(start_date=TODAY-timedelta(days=1), end_date=TODAY)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len({r['source_ref'] for r in response.data['results']}), 2)
        response = self.get(start_date=old.report.date, end_date=old.report.date)
        self.assertEqual(response.data['results'][0]['report_item_id'], old.pk)

    def test_week_starts_saturday_and_ends_today(self):
        for offset in (0, 4, 5):
            self.activity(day=TODAY-timedelta(days=offset))
        response = self.get(period='week')
        self.assertEqual(response.data['range']['start_date'], '2026-10-17')
        self.assertEqual(response.data['range']['end_date'], str(TODAY))
        self.assertEqual(response.data['count'], 2)
        with patch('apps.exams.study_sources.report_today', return_value=date(2026,10,17)):
            self.assertEqual(self.get(period='week').data['range']['start_date'], '2026-10-17')

    def test_default_date_uses_tehran_not_host_timezone(self):
        self.today_patch.stop()
        for instant, expected in ((datetime(2026,10,20,20,29,tzinfo=timezone.utc),'2026-10-20'),
                                  (datetime(2026,10,20,20,31,tzinfo=timezone.utc),'2026-10-21')):
            with patch('apps.daily_reports.services.timezone.now', return_value=instant):
                self.assertEqual(self.get().data['range']['start_date'], expected)

    def test_invalid_ranges_and_parameters(self):
        for params in ({'start_date':str(TODAY)}, {'period':'custom'}, {'period':'invalid'},
            {'start_date':'invalid','end_date':str(TODAY)},
            {'start_date':str(TODAY),'end_date':str(TODAY-timedelta(days=1))},
            {'start_date':str(TODAY),'end_date':str(TODAY+timedelta(days=1))},
            {'start_date':str(TODAY-timedelta(days=90)),'end_date':str(TODAY)},
            {'period':'week','start_date':str(TODAY),'end_date':str(TODAY)}):
            self.assertEqual(self.get(**params).status_code, 400, params)

    def test_nonacademic_events_and_missing_student_profile(self):
        item, row = self.activity()
        PlanItem.objects.filter(pk=item.pk).update(kind='EVENT')
        self.assertEqual(self.get().data['count'], 0)
        user = User.objects.create_user('source-no-profile', role='STUDENT')
        self.client.force_authenticate(user)
        self.assertEqual(self.get().status_code, 404)

    def test_unplanned_null_status_and_deleted_reports_are_excluded(self):
        report = DailyReport.objects.create(student=self.student, date=TODAY)
        for status in (None, 'NOT_DONE', 'COMPLETED'):
            DailyReportItem.objects.create(report=report, kind='STUDY', subject=self.subject,
                actual_duration_minutes=60, duration_source='MANUAL', reported_status=status)
        self.assertEqual(self.get().data['count'], 1)
        report.delete()
        self.assertEqual(self.get().data['count'], 0)

    def test_student_counselor_admin_and_reassignment_permissions(self):
        self.activity()
        for params in ({'student_id':self.student.pk}, {'student_id':self.other.pk}, {'student':self.other.pk}):
            self.assertEqual(self.get(**params).status_code, 403)
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.get().data['count'], 0)
        self.client.force_authenticate(self.counselor_user)
        self.assertEqual(self.get().data['count'], 1)
        self.assertEqual(self.get(student_id=self.other.pk).status_code, 404)
        self.assertEqual(self.get(student_id=self.student.pk).data['count'], 1)
        StudentProfile.objects.filter(pk=self.student.pk).update(counselor=self.other_counselor)
        self.assertEqual(self.get(student_id=self.student.pk).status_code, 404)
        self.client.force_authenticate(self.other_counselor_user)
        self.assertEqual(self.get(student_id=self.student.pk).data['count'], 1)
        admin = User.objects.create_user('source-admin', role='ADMIN')
        self.client.force_authenticate(admin)
        self.assertEqual(self.get(student_id=self.student.pk).data['count'], 1)
        self.client.force_authenticate(None)
        self.assertIn(self.get().status_code, (401,403))

    def test_pagination_counts_and_constant_query_budget(self):
        for _ in range(25):
            self.activity(topic=self.topic)
        with CaptureQueriesContext(connection) as queries:
            response = self.get(page_size=5)
        self.assertEqual(response.data['count'], 25)
        self.assertEqual(len(response.data['results']), 5)
        self.assertEqual(response.data['summary']['by_status']['COMPLETED'], 25)
        self.assertEqual(response.data['page_summary']['by_classification'], {'STRUCTURED': 5})
        with CaptureQueriesContext(connection) as larger:
            large = self.get(page_size=1000)
        self.assertEqual(len(large.data['results']), 25)
        self.assertEqual(len(queries), len(larger))
        self.assertLessEqual(len(larger), 6)
        second = self.client.get(response.data['next'])
        self.assertFalse({r['source_ref'] for r in response.data['results']} & {r['source_ref'] for r in second.data['results']})
        self.assertEqual(self.get(page=999).status_code, 404)

    def test_counselor_default_scope_batches_students_and_never_merges_their_permissions(self):
        self.activity(subject=self.subject)
        # Another assigned student is in a different educational period. Their
        # imported context must not borrow the first student's authorization.
        grade = Grade.objects.create(name='هشتم', number=8)
        StudentProfile.objects.filter(pk=self.other.pk).update(counselor=self.counselor, grade=grade)
        report = DailyReport.objects.create(student=self.other, date=TODAY)
        DailyReportItem.objects.create(report=report, kind='STUDY', subject=self.subject,
            actual_duration_minutes=10, duration_source='MANUAL', reported_status='COMPLETED')
        self.client.force_authenticate(self.counselor_user)
        with CaptureQueriesContext(connection) as queries:
            response = self.get()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data['student_id'])
        self.assertEqual(response.data['count'], 2)
        rows = {r['student_id']:r for r in response.data['results']}
        self.assertTrue(rows[self.student.pk]['academic_context_usable'])
        self.assertFalse(rows[self.other.pk]['academic_context_usable'])
        self.assertLessEqual(len(queries), 5)
        StudentProfile.objects.filter(pk=self.other.pk).update(counselor=self.other_counselor)
        self.assertEqual(self.get().data['count'], 1)
