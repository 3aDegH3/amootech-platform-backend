import io
import os
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from apps.accounts.models import User, StudentProfile
from apps.core.management.commands import audit_quiz24_taxonomy
from apps.daily_reports.models import DailyReportItem
from apps.planning.models import PlanItem
from apps.daily_reports.study_sources import eligible_sources, load_context, extract_sources


@override_settings(DEBUG=True)
class DemoSeedTests(TestCase):
    def setUp(self):
        import json
        from pathlib import Path
        self.capture = json.loads((Path(__file__).parents[1] / 'demo_catalog.json').read_text())
        def catalog(method, **kwargs):
            if method == 'course_detail':
                return self.capture['details'][str(kwargs['course_id'])]
            return self.capture[method]
        for target in ('apps.academics.provider_catalog.catalog',
                       'apps.exams.services.report_topics.catalog', 'apps.exams.services.catalog.catalog'):
            self.enterContext(patch(target, side_effect=catalog))
        self.enterContext(patch.object(audit_quiz24_taxonomy, 'catalog', side_effect=catalog))
        self.enterContext(patch('httpx.Client.post', side_effect=AssertionError('Unexpected provider network call')))

    def seed(self):
        with patch.dict(os.environ, ALLOW_DEMO_SEED='true'):
            call_command('seed_demo', counselors=1, students_per_counselor=1,
                         history_days=8, stdout=io.StringIO())

    def test_guard(self):
        with patch.dict(os.environ, ALLOW_DEMO_SEED=''):
            with self.assertRaises(CommandError):
                self.seed_disabled()
        with override_settings(DEBUG=False), self.assertRaises(CommandError):
            self.seed()

    def seed_disabled(self):
        call_command('seed_demo', stdout=io.StringIO())

    def test_reserved_real_user_is_preserved(self):
        real = User.objects.create_user('demo_student', email='real@example.com', role='STUDENT')
        with self.assertRaises(CommandError):
            self.seed()
        real.refresh_from_db()
        self.assertEqual(real.email, 'real@example.com')

    def test_rerun_and_full_preview_flow(self):
        self.seed()
        student = StudentProfile.objects.get(user__username='demo_student')
        self.assertEqual(student.grade.number, 12)
        self.assertGreaterEqual(len({item.taxonomy['course'] for item in PlanItem.objects.all()}), 2)
        statuses = set(DailyReportItem.objects.values_list('reported_status', flat=True))
        self.assertEqual(statuses, {'COMPLETED', 'PARTIAL', 'NOT_DONE', None})
        counts = (PlanItem.objects.count(), DailyReportItem.objects.count())
        self.seed()
        self.assertEqual(counts, (PlanItem.objects.count(), DailyReportItem.objects.count()))
        from apps.core.management.commands.seed_demo import Command
        import json
        from pathlib import Path
        capture = json.loads((Path(__file__).parents[1] / 'demo_catalog.json').read_text())
        def catalog(method, **kwargs):
            if method == 'course_detail':
                return capture['details'][str(kwargs['course_id'])]
            return capture[method]
        client = APIClient(); client.force_authenticate(student.user)
        with patch('apps.academics.provider_catalog.catalog', side_effect=catalog), patch('apps.exams.services.report_topics.catalog', side_effect=catalog), patch('apps.exams.services.catalog.catalog', side_effect=catalog), patch('apps.exams.services.quiz24.Quiz24Service.question_count', return_value={'result':{'easyCount':10,'mediumCount':0,'hardCount':0,'veryHardCount':0}}) as count:
            response = client.get('/api/v1/exams/study-sources/?period=today')
            self.assertEqual(response.status_code, 200, response.data)
            source = response.data['results'][0]
            self.assertIn(source['reported_status'], ('COMPLETED','PARTIAL'))
            refs = [{'report_item_id':source['report_item_id'], 'source_revision':source['source_revision']}]
            resolved = client.post('/api/v1/exams/study-sources/resolve/', {'sources':refs}, format='json')
            self.assertEqual(resolved.status_code,200,resolved.data)
            suggestion = resolved.data['sources'][0]['resolution']['suggestion']
            self.assertIsNotNone(suggestion)
            topic = suggestion['topics'][0]
            payload = {'sources':refs,'grade_id':suggestion['grade_id'],'course_id':suggestion['course_id'], 'topics':[{'level':topic['level'],'id':topic['id']}], 'confirmed':True,'question_type':1}
            preview = client.post('/api/v1/exams/study-sources/preview/',payload,format='json')
            self.assertEqual(preview.status_code,200,preview.data)
            self.assertFalse(preview.data['exam_created'])
            self.assertEqual(count.call_args.kwargs,preview.data['provider_filters'])
            payload['confirmed']=False
            self.assertEqual(client.post('/api/v1/exams/study-sources/preview/',payload,format='json').status_code,400)
            output = io.StringIO(); call_command('audit_quiz24_taxonomy', stdout=output)
            self.assertIn('Audit problems: 0',output.getvalue())

    def test_unavailable_live_catalog_preserves_existing_demo_data(self):
        from apps.exams.exceptions import Quiz24Error
        self.seed()
        before = list(PlanItem.objects.values_list('pk', 'taxonomy'))
        with patch('apps.academics.provider_catalog.catalog', side_effect=Quiz24Error(None, 'offline')):
            with self.assertRaises(CommandError):
                self.seed()
        self.assertEqual(list(PlanItem.objects.values_list('pk', 'taxonomy')), before)

    def test_stale_capture_preserves_existing_demo_data(self):
        import copy
        self.seed()
        before = list(PlanItem.objects.values_list('pk', 'taxonomy'))
        def catalog(method, **kwargs):
            if method == 'course_detail':
                return {'result': []}
            return copy.deepcopy(self.capture[method])
        with patch('apps.academics.provider_catalog.catalog', side_effect=catalog), patch('apps.exams.services.report_topics.catalog', side_effect=catalog):
            with self.assertRaises(CommandError):
                self.seed()
        self.assertEqual(list(PlanItem.objects.values_list('pk', 'taxonomy')), before)

    def test_audit_classifies_missing_invalid_ambiguous_and_broken_links(self):
        import json
        self.seed()
        items = list(PlanItem.objects.order_by('pk')[:3])
        PlanItem.objects.filter(pk=items[0].pk).update(taxonomy={})
        PlanItem.objects.filter(pk=items[1].pk).update(taxonomy={'provider':'quiz24','grade':31,'course':999999,'chapter':1})
        duplicate = dict(self.capture['courses']['result'][0])
        self.capture['courses']['result'].append(duplicate)
        row = DailyReportItem.objects.filter(plan_item=items[2]).first()
        DailyReportItem.objects.filter(pk=row.pk).update(reported_status='BAD')
        output = io.StringIO()
        call_command('audit_quiz24_taxonomy', json=True, stdout=output)
        summary = json.loads(output.getvalue())
        self.assertGreater(summary['plans']['valid'],0)
        self.assertEqual(summary['plans']['missing'],1)
        self.assertGreater(summary['plans']['invalid'],0)
        self.assertGreater(summary['plans']['ambiguous'],0)
        self.assertEqual(summary['integrity']['invalid_status'],1)
        self.assertEqual(summary['plans'],summary['demo_plans'])

    def test_audit_catalog_unavailable_fails_closed(self):
        from apps.exams.exceptions import Quiz24Error
        with patch('apps.academics.provider_catalog.catalog', side_effect=Quiz24Error(None, 'offline')), patch('apps.core.management.commands.audit_quiz24_taxonomy.catalog', side_effect=Quiz24Error(None, 'offline')):
            with self.assertRaises(CommandError):
                call_command('audit_quiz24_taxonomy', stdout=io.StringIO())
        self.assertEqual(PlanItem.objects.count(),0)
