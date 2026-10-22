from datetime import timedelta
from django.test import TestCase, TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.daily_reports.services import report_payload
from apps.planning.models import PlanItem, PlanItemExecution
from . import test_reports as legacy
BASE = legacy.BASE


class QuickReportTests(TestCase):
    setUp = legacy.DailyReportTests.setUp
    open = legacy.DailyReportTests.open

    def test_unset_three_states_and_idempotent_item_update(self):
        opened = self.open().data
        rid = opened['id']
        self.assertTrue(all(row['reported_status'] is None for row in opened['items']))
        self.assertEqual(opened['reporting_progress'], {'reported': 0, 'total': 3})
        endpoint = f'{BASE}{rid}/planned/{self.study.pk}/'
        before = DailyReport.objects.get(pk=rid)
        row_id = next(row['id'] for row in opened['items'] if row['plan_item'] == self.study.pk)
        for status in ('COMPLETED', 'PARTIAL', 'NOT_DONE', 'NOT_DONE', 'COMPLETED'):
            response = self.client.put(endpoint, {'reported_status': status}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            row = next(row for row in response.data['items'] if row['id'] == row_id)
            self.assertEqual(row['reported_status'], status)
            self.assertEqual(row['execution_status'], 'NOT_STARTED')
            self.assertIsNone(row['actual_duration_minutes'])
            self.assertIsNone(row['actual_test_count'])
            self.assertEqual(response.data['reporting_progress'], {'reported': 1, 'total': 3})
        self.assertEqual(DailyReportItem.objects.filter(report_id=rid, plan_item=self.study).count(), 1)
        self.assertFalse(PlanItemExecution.objects.filter(plan_item=self.study).exists())
        after = DailyReport.objects.get(pk=rid)
        self.assertEqual((after.created_at, after.updated_at), (before.created_at, before.updated_at))
        self.assertEqual(self.client.put(endpoint, {'reported_status': 'INVALID'}, format='json').status_code, 400)
        self.assertEqual(self.client.put(f'{BASE}{rid}/planned/not-an-id/', {'reported_status':'COMPLETED'}, format='json').status_code,400)

    def test_status_preserves_optional_measurements_and_other_rows(self):
        rid = self.open().data['id']
        row = DailyReportItem.objects.get(report_id=rid, plan_item=self.test_item)
        row.actual_duration_minutes = 35
        row.duration_source = 'MANUAL'
        row.actual_test_count = 12
        row.correct_count, row.wrong_count, row.unanswered_count = 8, 3, 1
        row.note = 'یادداشت قبلی'
        row.save()
        endpoint = f'{BASE}{rid}/planned/{self.test_item.pk}/'
        for status in ('COMPLETED', 'NOT_DONE', 'PARTIAL'):
            response = self.client.put(endpoint, {'reported_status': status}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            row.refresh_from_db()
            self.assertEqual((row.actual_duration_minutes, row.actual_test_count, row.correct_count, row.wrong_count, row.unanswered_count, row.note), (35, 12, 8, 3, 1, 'یادداشت قبلی'))
            self.assertEqual(response.data['summary']['total_actual_minutes'], 35)
            self.assertEqual(response.data['summary']['total_tests'], 12)
        self.assertIsNone(DailyReportItem.objects.get(report_id=rid, plan_item=self.study).reported_status)

    def test_status_context_analytics_no_inferred_topic_coverage(self):
        free = PlanItem.objects.create(plan_day=self.day, kind='STUDY', title='مرور آزاد', planned_duration_minutes=30)
        structured = PlanItem.objects.create(plan_day=self.day, kind='REVIEW', title='عنوان سفارشی', planned_duration_minutes=20,
            taxonomy={'provider':'quiz24','course':900,'object':903,'labels':{'course':'فیزیک','object':'حرکت'}})
        rid = self.open().data['id']
        for item, status in ((free,'COMPLETED'), (structured,'PARTIAL'), (self.study,'COMPLETED')):
            response = self.client.put(f'{BASE}{rid}/planned/{item.pk}/', {'reported_status':status}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
        structured.refresh_from_db()
        self.assertEqual(structured.title,'عنوان سفارشی')
        self.assertEqual(structured.taxonomy['object'],903)
        response = self.client.get('/api/v1/student/reports/', {'start_date':str(self.date),'end_date':str(self.date)})
        self.assertEqual(response.data['summary']['completed_blocks'],2)
        self.assertEqual(response.data['summary']['actual_minutes'],0)
        self.assertEqual(response.data['topic_repetition'][0]['actual'],0)
        self.assertEqual(self.client.get(f'{BASE}{rid}/').data['reporting_progress']['reported'],3)

    def test_permissions_dates_and_historical_unset(self):
        rid = self.open().data['id']
        endpoint = f'{BASE}{rid}/planned/{self.study.pk}/'
        for user in (self.other_user, self.counselor_user):
            self.client.force_authenticate(user)
            self.assertIn(self.client.put(endpoint, {'reported_status':'COMPLETED'}, format='json').status_code,(403,404))
        self.client.force_authenticate(self.user)
        other_report = DailyReport.objects.create(student=self.other_student,date=self.date)
        self.assertEqual(self.client.put(f'{BASE}{other_report.pk}/planned/{self.study.pk}/',{'reported_status':'COMPLETED'},format='json').status_code,404)
        yesterday = DailyReport.objects.create(student=self.student,date=self.date-timedelta(days=1))
        self.assertEqual(self.client.put(f'{BASE}{yesterday.pk}/planned/{self.study.pk}/',{'reported_status':'COMPLETED'},format='json').status_code,400)
        old = DailyReport.objects.create(student=self.student,date=self.date-timedelta(days=40))
        self.assertEqual(self.client.put(f'{BASE}{old.pk}/planned/{self.study.pk}/',{'reported_status':'COMPLETED'},format='json').status_code,400)
        self.assertFalse(report_payload(old)['editable'])
        self.assertEqual(self.client.post(f'{BASE}open/',{'date':str(self.date+timedelta(days=1))},format='json').status_code,400)
        # Positive historical metrics do not imply a submitted completion declaration.
        row=DailyReportItem.objects.get(report_id=rid,plan_item=self.study)
        row.actual_duration_minutes=20;row.duration_source='MANUAL';row.save()
        payload=self.client.get(f'{BASE}{rid}/').data
        self.assertIsNone(next(r for r in payload['items'] if r['id']==row.pk)['reported_status'])
        self.assertEqual(payload['reporting_progress']['reported'],0)

    def test_editable_past_date_syncs_missing_activities_and_preserves_history(self):
        from apps.planning.models import PlanDay
        self.plan.start_date = self.date - timedelta(days=2)
        self.plan.save()
        day = PlanDay.objects.create(plan=self.plan, date=self.date-timedelta(days=1))
        item = PlanItem.objects.create(plan_day=day, kind='STUDY', title='روز قبل', planned_duration_minutes=20)
        historical = DailyReport.objects.create(student=self.student, date=day.date, note='قبلی')
        opened = self.client.post(f'{BASE}open/', {'date':str(day.date)}, format='json')
        self.assertEqual(opened.status_code,200,opened.data)
        self.assertEqual(opened.data['note'],'قبلی')
        self.assertTrue(opened.data['editable'])
        response=self.client.put(f'{BASE}{historical.pk}/planned/{item.pk}/',{'reported_status':'PARTIAL'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['reporting_progress'],{'reported':1,'total':1})

    def test_running_timer_is_not_changed_by_reporting(self):
        rid=self.open().data['id']
        self.client.post(f'/api/v1/planning/items/{self.study.pk}/start/',{},format='json')
        execution=PlanItemExecution.objects.get(plan_item=self.study)
        started=execution.current_session_started_at
        response=self.client.put(f'{BASE}{rid}/planned/{self.study.pk}/',{'reported_status':'NOT_DONE'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        execution.refresh_from_db()
        self.assertEqual(execution.status,'IN_PROGRESS')
        self.assertEqual(execution.current_session_started_at,started)


class QuickReportMigrationTests(TransactionTestCase):
    setUp = legacy.DailyReportTests.setUp

    def test_additive_migration_preserves_history(self):
        report=DailyReport.objects.create(student=self.student,date=self.date,note='historical note')
        row=DailyReportItem.objects.create(report=report,plan_item=self.study,actual_duration_minutes=25,duration_source='MANUAL',note='old item')
        original=(report.created_at,report.updated_at)
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([('daily_reports','0004_dailyreport_source_dailyreportitem_source')])
            MigrationExecutor(connection).migrate(latest)
            report.refresh_from_db();row.refresh_from_db()
            self.assertEqual((report.created_at,report.updated_at),original)
            self.assertEqual(row.note,'old item')
            self.assertEqual(row.actual_duration_minutes,25)
            self.assertEqual(row.plan_item_id,self.study.pk)
            self.assertIsNone(row.reported_status)
        finally:
            MigrationExecutor(connection).migrate(latest)
