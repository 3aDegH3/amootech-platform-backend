from unittest.mock import patch

from django.core.cache import cache
from rest_framework.test import APITestCase
from . import test_study_sources as extraction
from apps.daily_reports.models import DailyReportItem
from apps.planning.models import PlanItem
from apps.exams.models import Exam, ExamCreation
from apps.accounts.models import StudentProfile

BASE='/api/v1/exams/study-sources/'


class ReportTopicTests(APITestCase):
    activity = extraction.StudySourceTests.activity
    get = extraction.StudySourceTests.get

    def setUp(self):
        extraction.StudySourceTests.setUp(self)
        cache.clear()
        # Sprint 5 deliberately blocks the adapter. This sprint allows only mocked reads.
        self.catalog_patch=patch('apps.academics.provider_catalog.catalog', side_effect=self.catalog)
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.count=patch('apps.exams.services.quiz24.Quiz24Service.question_count',return_value={
            'result':{'easyCount':4,'mediumCount':3,'hardCount':2,'veryHardCount':1},
        }).start()
        self.create=patch('apps.exams.services.quiz24.Quiz24Service.create_online_exam',side_effect=AssertionError('No creation allowed')).start()
        # catalog.py imports its adapter function directly.
        patch('apps.exams.services.catalog.catalog',side_effect=self.catalog).start()
        patch('apps.exams.services.report_topics.catalog',side_effect=self.catalog).start()
        self.addCleanup(patch.stopall)

    def catalog(self,method,**kwargs):
        if method=='grades':
            return {'result':[{'gradeId':700,'gradeName':'دوازدهم'},{'gradeId':709,'gradeName':'نهم'}]}
        if method=='courses':
            return {'result':[{'courseId':900,'courseName':'حسابان','gradeId':700,'fieldName':'ریاضی'},
                              {'courseId':910,'courseName':'فیزیک','gradeId':700,'fieldName':'ریاضی'},
                              {'courseId':999,'courseName':'علوم','gradeId':709},
                              {'courseId':920,'courseName':'زیست','gradeId':700,'fieldName':'تجربی'}]}
        if method=='course_detail':
            course=kwargs['course_id']
            def n(key,level,parent,children=None):
                return {'key':key,'title':f'مبحث {key}','type':level,'parentId':parent,'children':children or []}
            return {'result':[n(course,1,None,[n(1,2,course,[n(2,3,1,[n(3,4,2),n(4,4,2)])]),n(5,2,course,[n(6,3,5,[n(7,4,6)])])])]}
        raise AssertionError(method)

    def references(self):
        return [{'report_item_id':row['report_item_id'],'source_revision':row['source_revision']}
                for row in self.get().data['results']]

    def preview(self,**changes):
        return self.client.post(BASE+'preview/',{'sources':self.references(),'grade_id':700,'course_id':900,
            'topics':[{'level':'object','id':3}],'confirmed':True,'question_type':1,**changes},format='json')

    def test_resolve_course_chapter_subject_object_without_assuming_coverage(self):
        for ids in ({},{'chapter':1},{'subject':2},{'object':3}):
            self.activity(taxonomy={'provider':'quiz24','grade':700,'course':900,**ids,'labels':{}})
        response=self.client.post(BASE+'resolve/',{'sources':self.references()},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['default_grade_id'],700)
        suggestions=[row['resolution']['suggestion'] for row in response.data['sources']]
        self.assertEqual([row['topics'][0]['level'] if row['topics'] else 'course' for row in suggestions],['course','chapter','subject','object'])
        self.assertEqual(suggestions[-1]['topics'][0]['path'],{'course':900,'chapter':1,'subject':2,'object':3})
        self.count.assert_not_called();self.create.assert_not_called()

    def test_free_text_internal_ids_and_custom_title_are_not_provider_mappings(self):
        self.activity(subject=self.subject)
        item,_=self.activity()
        PlanItem.objects.filter(pk=item.pk).update(title='فصل 1 مبحث 3',title_is_custom=True)
        response=self.client.post(BASE+'resolve/',{'sources':self.references()},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertTrue(all(row['resolution']['suggestion'] is None for row in response.data['sources']))
        response=self.preview()
        self.assertEqual(response.status_code,200,response.data)
        self.assertTrue(response.data['warnings'])
        self.assertEqual(response.data['selected_topics'][0]['id'],3)

    def test_multi_topic_complete_filters_and_count_options(self):
        self.activity(status='PARTIAL')
        self.activity()
        response=self.preview(topics=[{'level':'object','id':3},{'level':'object','id':7}],
                              question_type=2,consider_used_question=True)
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['availability'],{'easy':4,'medium':3,'hard':2,'very_hard':1})
        self.assertEqual(self.count.call_args.kwargs,{'grade_id':700,'course_id':900,'question_type':2,
            'consider_used_question':True,'chapter_ids':[1,5],'subject_ids':[2,6],'object_ids':[3,7]})
        self.assertEqual(len(response.data['sources']),2)
        self.assertFalse(response.data['exam_created'])
        self.assertFalse(Exam.objects.exists());self.assertFalse(ExamCreation.objects.exists());self.create.assert_not_called()

    def test_overlap_is_normalized_once_and_mixed_disjoint_depths_rejected(self):
        self.activity()
        response=self.preview(topics=[{'level':'chapter','id':1},{'level':'subject','id':2},{'level':'object','id':3}])
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(len(response.data['selected_topics']),1)
        self.assertEqual(response.data['provider_filters']['subject_ids'],[])
        self.assertEqual(self.count.call_count,1)
        response=self.preview(topics=[{'level':'chapter','id':1},{'level':'object','id':7}])
        self.assertEqual(response.status_code,400,response.data)
        self.assertEqual(self.count.call_count,1)

    def test_invalid_or_empty_scope_and_unauthorized_courses(self):
        self.activity()
        for changes in ({'sources':[]},{'topics':[]},{'confirmed':False},{'topics':[{'level':'object','id':999}]},
            {'topics':[{'level':'object','id':3},{'level':'object','id':3}]},
            {'grade_id':709,'course_id':999},{'course_id':999},{'course_id':920},
            {'student_id':self.student.pk},{'course_id':[900,910]}):
            self.assertIn(self.preview(**changes).status_code,(400,403),changes)
        self.count.assert_not_called();self.create.assert_not_called()

    def test_foreign_and_forged_sources_are_rejected(self):
        _,row=self.activity()
        references=self.references()
        forged=[{**references[0],'report_item_id':999999}]
        self.assertEqual(self.preview(sources=forged).status_code,400)
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.preview(sources=references).status_code,400)
        self.client.force_authenticate(self.counselor_user)
        self.assertEqual(self.preview(sources=references).status_code,403)
        self.count.assert_not_called()

    def test_stale_status_title_or_taxonomy_is_rejected(self):
        item,row=self.activity()
        references=self.references()
        for status in ('NOT_DONE',None,'PARTIAL'):
            DailyReportItem.objects.filter(pk=row.pk).update(reported_status=status)
            self.assertEqual(self.preview(sources=references).status_code,400)
        DailyReportItem.objects.filter(pk=row.pk).update(reported_status='COMPLETED')
        PlanItem.objects.filter(pk=item.pk).update(title='تغییر عنوان')
        self.assertEqual(self.preview(sources=references).status_code,400)
        self.count.assert_not_called()

    def test_report_change_during_count_prevents_stale_preview(self):
        _,row=self.activity()
        def count(**kwargs):
            DailyReportItem.objects.filter(pk=row.pk).update(reported_status='NOT_DONE')
            return {'result':{'easyCount':1,'mediumCount':0,'hardCount':0,'veryHardCount':0}}
        self.count.side_effect=count
        self.assertEqual(self.preview().status_code,400)
        self.create.assert_not_called()

    def test_saved_context_with_mismatched_or_removed_ancestry_needs_selection(self):
        for ids in ({'chapter':5,'subject':2},{'object':999},{'grade':709},{'course':999}):
            self.activity(taxonomy={'provider':'quiz24','course':900,**ids})
        response=self.client.post(BASE+'resolve/',{'sources':self.references()},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertTrue(all(row['resolution']['suggestion'] is None for row in response.data['sources']))

    def test_multiple_course_suggestions_and_explicit_correction_are_course_scoped(self):
        self.activity(taxonomy={'provider':'quiz24','course':900,'chapter':1})
        self.activity(taxonomy={'provider':'quiz24','course':910,'object':3})
        response=self.client.post(BASE+'resolve/',{'sources':self.references()},format='json')
        self.assertEqual({row['resolution']['suggestion']['course_id'] for row in response.data['sources']},{900,910})
        references=self.references()
        response=self.preview(sources=references[1:],course_id=910)
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(self.count.call_args.kwargs['course_id'],910)
        self.assertEqual(len(response.data['sources']),1)

    def test_malformed_and_zero_inventory_never_creates_or_broadens(self):
        self.activity()
        for inventory in (None,{},[],{'result':{'easyCount':False}}):
            self.count.return_value=inventory
            self.assertEqual(self.preview().status_code,502)
        self.count.return_value={'result':{'easyCount':0,'mediumCount':0,'hardCount':0,'veryHardCount':0}}
        response=self.preview()
        self.assertEqual(response.status_code,200,response.data)
        self.assertIn('سؤال قابل استفاده‌ای موجود نیست',' '.join(response.data['warnings']))
        self.assertEqual(response.data['provider_filters']['object_ids'],[3])
        self.create.assert_not_called()

    def test_malformed_tree_or_supplied_ancestor_forgery_is_rejected(self):
        self.activity()
        response=self.preview(topics=[{'level':'object','id':3,'chapter':5}])
        self.assertEqual(response.status_code,400,response.data)
        original=self.catalog
        for tree in ({'result':{}},{'result':{'courseId':910,'chapters':[]}},
                     {'result':{'gradeId':709,'chapters':[]}},
                     {'result':[{'key':900,'type':1,'children':[
            {'key':1,'type':2,'title':'فصل','parentId':999,'children':[]}]}]}):
            with patch('apps.exams.services.report_topics.catalog',side_effect=lambda method,**kw:tree if method=='course_detail' else original(method,**kw)):
                self.assertEqual(self.preview().status_code,400)
        self.count.assert_not_called();self.create.assert_not_called()

    def test_changed_profile_during_count_cannot_validate_old_scope(self):
        self.activity()
        def count(**kwargs):
            StudentProfile.objects.filter(pk=self.student.pk).update(grade=None,field=None)
            return {'result':{'easyCount':1,'mediumCount':0,'hardCount':0,'veryHardCount':0}}
        self.count.side_effect=count
        self.assertIn(self.preview().status_code,(400,403))
        self.create.assert_not_called()

    def test_corrected_course_keeps_original_provenance_and_warns(self):
        self.activity(taxonomy={'provider':'quiz24','course':910,'subject':2})
        response=self.preview(course_id=900)
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['scope_origin'],'student_confirmed')
        self.assertIn('متفاوت است',' '.join(response.data['warnings']))
        self.assertEqual(response.data['sources'][0]['taxonomy_snapshot']['provider_context']['course'],910)
        self.assertEqual(response.data['source_links'][0]['topics'],[{'level':'object','id':3}])

    def test_normalized_tree_can_have_same_identifiers_at_different_levels(self):
        self.activity()
        original=self.catalog
        tree={'result':{'chapters':[{'id':1,'title':'فصل','subjects':[{'id':1,'title':'موضوع','objects':[{'id':1,'title':'زیرمبحث'}]}]}]}}
        def catalog(method,**kwargs):
            return tree if method=='course_detail' else original(method,**kwargs)
        with patch('apps.exams.services.report_topics.catalog',side_effect=catalog),patch('apps.exams.services.catalog.catalog',side_effect=catalog):
            response=self.preview(topics=[{'level':'object','id':1}])
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['selected_topics'][0]['label'],'زیرمبحث')


    def test_internal_report_resolves_only_unique_authorized_catalog_path(self):
        from apps.academics.models import Chapter, Topic
        Chapter.objects.filter(pk=self.chapter.pk).update(name='مبحث 1')
        Topic.objects.filter(pk=self.topic.pk).update(name='مبحث 2')
        _, source = self.activity(topic=self.topic)
        response = self.client.get(BASE+'topics/')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['eligible_sources'],1)
        group = response.data['groups'][0]
        self.assertEqual((group['grade_id'],group['course_id']),(700,900))
        node = next(n for n in group['topics'] if n['level']=='object')
        self.assertEqual(node['path'],{'course':900,'chapter':1,'subject':2,'object':3})
        self.assertEqual(node['sources'][0]['report_item_id'],source.pk)
        context={'sources':node['sources'],'grade_id':700,'course_id':900,
            'topics':[{'level':'object','id':3}],'confirmed':True,'question_type':1}
        self.assertEqual(self.client.post(BASE+'topics/preview/',context,format='json').status_code,200)
        self.create.assert_not_called()

    def test_unmapped_report_is_distinct_from_empty_date_range(self):
        self.activity(topic=self.topic)
        data = self.client.get(BASE+'topics/').data
        self.assertEqual(data['groups'],[])
        self.assertEqual((data['eligible_sources'],data['unavailable_sources']),(1,1))
        data = self.client.get(BASE+'topics/',{'period':'custom','start_date':'2026-10-20','end_date':'2026-10-20'}).data
        self.assertEqual((data['eligible_sources'],data['unavailable_sources']),(0,0))
        self.assertEqual(data['groups'],[])

    def test_internal_path_does_not_guess_missing_or_ambiguous_catalog_matches(self):
        from apps.academics.models import Chapter, Topic
        Chapter.objects.filter(pk=self.chapter.pk).update(name='مبحث 1')
        Topic.objects.filter(pk=self.topic.pk).update(name='مبحث 7')
        self.activity(topic=self.topic)
        # A matching label outside the exact saved parent path never grants access.
        self.assertEqual(self.client.get(BASE+'topics/').data['groups'],[])

    def test_ambiguous_internal_path_is_not_mapped(self):
        from apps.academics.models import Chapter, Topic
        Chapter.objects.filter(pk=self.chapter.pk).update(name='مبحث 1')
        Topic.objects.filter(pk=self.topic.pk).update(name='مبحث 2')
        self.activity(topic=self.topic)
        original = self.catalog
        def duplicate(method, **kwargs):
            data = original(method, **kwargs)
            if method == 'course_detail':
                chapters = data['result'][0]['children']
                chapters[1]['title'] = 'مبحث 1'
                chapters[1]['children'][0]['title'] = 'مبحث 2'
            return data
        with patch('apps.exams.services.report_topics.catalog', side_effect=duplicate):
            self.assertEqual(self.client.get(BASE+'topics/').data['groups'], [])
