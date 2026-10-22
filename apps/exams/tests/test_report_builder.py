"""Recorded live read-only catalog; all provider mutations are mocked."""
import json
from pathlib import Path
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch
from django.db import DatabaseError
from django.utils import timezone
from rest_framework.test import APITestCase
from apps.accounts.models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field
from apps.daily_reports.models import DailyReport, DailyReportItem
from apps.planning.models import Plan, PlanDay, PlanItem
from apps.exams.models import Exam, ExamCreation
from apps.exams.services.catalog import taxonomy_index
from apps.daily_reports.services import report_today

BASE='/api/v1/exams/'


class ReportBuilderTests(APITestCase):
    def setUp(self):
        self.capture=json.loads((Path(__file__).parent/'fixtures/report_catalog.json').read_text())
        self.user=User.objects.create_user('report-builder',role='STUDENT')
        grade=Grade.objects.create(number=12,name='دوازدهم')
        field=Field.objects.create(grade=grade,name='تجربی')
        self.student=StudentProfile.objects.create(user=self.user,grade=grade,field=field)
        counselor=CounselorProfile.objects.create(user=User.objects.create_user('builder-counselor',role='COUNSELOR'))
        self.student.counselor=counselor;self.student.save()
        today=report_today()
        self.plan=Plan.objects.create(student=self.student,counselor=counselor,status='PUBLISHED',published_at=timezone.now(),start_date=today-timedelta(days=30),end_date=today)
        self.client.force_authenticate(self.user)
        self.course=int(next(iter(self.capture['details'])))
        self.index=taxonomy_index(self.capture['details'][str(self.course)],self.course,31)
        self.chapter=next(iter(self.index['chapter']))
        self.object=next(i for i,path in self.index['object'].items() if path['chapter']==self.chapter)
        for target in ('apps.academics.provider_catalog.catalog','apps.exams.services.report_topics.catalog','apps.exams.services.catalog.catalog'):
            self.enterContext(patch(target,side_effect=self.catalog))
        self.count=self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.question_count',return_value={'result':{'easyCount':4,'mediumCount':2,'hardCount':1,'veryHardCount':0}}))
        self.create=self.enterContext(patch('apps.exams.services.quiz24.Quiz24Service.create_online_exam',return_value={'status':100,'result':{'examCode':'actual-code','examId':'actual-id'}}))
        self.enterContext(patch('apps.exams.services.quiz24.student_quiz24_mobile',return_value='09123456789'))
        self.enterContext(patch('httpx.Client.post',side_effect=AssertionError('Network mutation forbidden')))

    def catalog(self,method,**kwargs):
        return self.capture['details'][str(kwargs['course_id'])] if method=='course_detail' else self.capture[method]

    def activity(self,status='COMPLETED',date=None,course=None,taxonomy=None):
        date=date or report_today(); course=course or self.course
        index=taxonomy_index(self.capture['details'][str(course)],course,31)
        chapter=next(iter(index['chapter']))
        day,_=PlanDay.objects.get_or_create(plan=self.plan,date=date)
        item=PlanItem.objects.create(plan_day=day,kind='STUDY',title='مطالعه',planned_duration_minutes=60,
            taxonomy=taxonomy if taxonomy is not None else {'provider':'quiz24','grade':31,'course':course,'chapter':chapter,'labels':{}})
        report,_=DailyReport.objects.get_or_create(student=self.student,date=date)
        return DailyReportItem.objects.create(report=report,plan_item=item,reported_status=status)

    def groups(self,query='period=today'):
        response=self.client.get(BASE+'study-sources/topics/?'+query)
        self.assertEqual(response.status_code,200,response.data)
        return response.data

    def context(self):
        group=self.groups()['groups'][0]
        node=next(n for n in group['topics'] if n['level']=='object')
        return {'sources':node['sources'],'grade_id':31,'course_id':self.course,'topics':[{'level':'object','id':node['id']}], 'confirmed':True,'question_type':1,'consider_used_question':False}

    def payload(self,context=None):
        context=context or self.context()
        preview=self.client.post(BASE+'study-sources/topics/preview/',context,format='json')
        self.assertEqual(preview.status_code,200,preview.data)
        filters=preview.data['provider_filters'].copy();filters['grade_external_id']=filters.pop('grade_id')
        return {**filters,'report_context':context,'creation_key':str(uuid4()),'title':'آزمون گزارش کار','duration_minutes':20,'easy_count':2}

    def post(self,payload):
        return self.client.post(BASE+'automatic/',payload,format='json')

    def test_dates_eligibility_grouping_dedup_real_children_and_legacy(self):
        first=self.activity();second=self.activity('PARTIAL')
        self.activity('NOT_DONE');self.activity(None);self.activity(taxonomy={})
        data=self.groups();self.assertEqual(data['unavailable_sources'],1)
        self.assertEqual(len(data['groups']),1)
        nodes=data['groups'][0]['topics']
        self.assertTrue(any(n['level']=='object' for n in nodes))
        self.assertEqual(len(nodes),len({(n['level'],n['id']) for n in nodes}))
        for node in nodes:
            self.assertEqual({r['report_item_id'] for r in node['sources']},{first.pk,second.pk})
            self.assertEqual(node['path']['chapter'],self.chapter)
        historical=report_today()-timedelta(days=8)
        self.activity(date=historical)
        self.assertEqual(len(self.groups()['groups'][0]['topics'][0]['sources']),2)
        custom=self.groups(f'period=custom&start_date={historical}&end_date={historical}')
        self.assertEqual(len(custom['groups'][0]['topics'][0]['sources']),1)
        self.assertEqual(len(self.groups('period=week')['groups'][0]['topics'][0]['sources']),2)
        self.create.assert_not_called()

    def test_create_exact_payload_persistence_retry_link_history_and_owner(self):
        self.activity();payload=self.payload()
        response=self.post(payload);self.assertEqual(response.status_code,201,response.data)
        exam=Exam.objects.get();self.assertEqual(exam.student,self.student)
        self.assertEqual(exam.provider_exam_code,'actual-code');self.assertEqual(exam.provider_exam_id,'actual-id')
        receipt=ExamCreation.objects.get();self.assertTrue(receipt.confirmed_scope['confirmed'])
        self.assertEqual(receipt.confirmed_scope['source_links'][0]['report_item_id'],payload['report_context']['sources'][0]['report_item_id'])
        kwargs=self.create.call_args.kwargs
        for key in ('course_id','chapter_ids','subject_ids','object_ids'):
            self.assertEqual(kwargs[key],payload[key])
        self.assertEqual(kwargs['grade_id'],31);self.assertEqual(kwargs['easy_count'],2)
        self.assertEqual(self.post(payload).status_code,200);self.create.assert_called_once()
        with patch('apps.exams.views.student_quiz24_mobile',return_value='09123456789'), patch('apps.exams.services.quiz24.Quiz24Service.get_participation_url',return_value='https://quiz24.ir/verified-link'):
            link=self.client.post(BASE+f'{exam.pk}/start/');self.assertEqual(link.status_code,200,link.data)
            self.assertEqual(link.data['participation_url'],'https://quiz24.ir/verified-link')
        self.assertEqual(self.client.get(BASE).data['results'][0]['id'],exam.pk)
        other=User.objects.create_user('foreign',role='STUDENT');StudentProfile.objects.create(user=other)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(BASE+f'{exam.pk}/').status_code,404)
        self.assertEqual(self.post(payload).status_code,400)

    def test_unconfirmed_stale_counts_forged_filters_and_outside_scope_rejected(self):
        report=self.activity();payload=self.payload()
        cases=[{**payload,'easy_count':5}, {**payload,'chapter_ids':[]},
               {**payload,'report_context':{**payload['report_context'],'confirmed':False}}]
        for bad in cases:self.assertEqual(self.post(bad).status_code,400)
        outside=next(i for i,path in self.index['object'].items() if path['chapter']!=self.chapter)
        context={**payload['report_context'],'topics':[{'level':'object','id':outside}]}
        self.assertEqual(self.client.post(BASE+'study-sources/topics/preview/',context,format='json').status_code,400)
        DailyReportItem.objects.filter(pk=report.pk).update(reported_status='NOT_DONE')
        self.assertEqual(self.post(payload).status_code,400)
        self.create.assert_not_called()

    def test_provider_success_local_failure_retry_only_finalizes_local(self):
        self.activity();payload=self.payload()
        with patch('apps.exams.views.Exam.objects.create',side_effect=DatabaseError('simulated')):
            self.assertEqual(self.post(payload).status_code,503)
        self.assertEqual(ExamCreation.objects.get().state,'RECEIVED')
        self.count.side_effect=AssertionError('No new inventory on recovery')
        self.assertEqual(self.post(payload).status_code,201)
        self.create.assert_called_once()

    def test_multiple_courses_create_separate_owned_exams(self):
        for course in self.capture['details']:self.activity(course=int(course))
        groups=self.groups()['groups'];self.assertEqual(len(groups),2)
        for index,group in enumerate(groups):
            node=next(n for n in group['topics'] if n['level']=='subject')
            context={'sources':node['sources'],'grade_id':31,'course_id':group['course_id'],'topics':[{'level':'subject','id':node['id']}],'confirmed':True,'question_type':1,'consider_used_question':False}
            self.create.return_value={'examCode':f'code-{index}','examId':f'id-{index}'}
            self.assertEqual(self.post(self.payload(context)).status_code,201)
        self.assertEqual(Exam.objects.filter(student=self.student).count(),2)

    def test_strict_preview_links_only_topics_covered_by_each_source(self):
        first=self.activity()
        second_object=next(i for i,path in self.index['object'].items() if path['chapter']!=self.chapter)
        second=self.activity(taxonomy={'provider':'quiz24','grade':31,'course':self.course,'object':second_object})
        group=self.groups()['groups'][0]
        nodes=[next(n for n in group['topics'] if n['level']=='object' and n['id']==identifier)
               for identifier in (self.object,second_object)]
        context={'sources':[ref for node in nodes for ref in node['sources']],
            'grade_id':31,'course_id':self.course,'topics':[{'level':'object','id':n['id']} for n in nodes],
            'confirmed':True,'question_type':1,'consider_used_question':False}
        response=self.client.post(BASE+'study-sources/topics/preview/',context,format='json')
        self.assertEqual(response.status_code,200,response.data)
        links={link['report_item_id']:link['topics'] for link in response.data['source_links']}
        self.assertEqual(links,{first.pk:[{'level':'object','id':self.object}],second.pk:[{'level':'object','id':second_object}]})

    def test_report_context_revalidated_after_inventory(self):
        source=self.activity();context=self.context()
        def change(**kwargs):
            DailyReportItem.objects.filter(pk=source.pk).update(reported_status='NOT_DONE')
            return {'result':{'easyCount':4,'mediumCount':2,'hardCount':1,'veryHardCount':0}}
        self.count.side_effect=change
        self.assertEqual(self.client.post(BASE+'study-sources/topics/preview/',context,format='json').status_code,400)
        self.create.assert_not_called()
