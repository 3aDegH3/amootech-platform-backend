from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsStudent
from apps.academics.provider_catalog import authorize_provider, option, field
from .exceptions import Quiz24Error, quiz24_error_data
from .services.automatic import normalize_inventory
from .services.quiz24 import Quiz24Service
from .services.catalog import validate_student_selection
from .services.report_topics import (
    SourceInput, ReportPreviewInput, source_snapshots, resolve_suggestion,
    tree_context, normalize_topics,
)


class ReportTopicResolveView(APIView):
    permission_classes = (IsStudent,)

    def post(self, request):
        serializer = SourceInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            sources = source_snapshots(request.user, serializer.validated_data['sources'])
            results = [{**source, 'resolution':resolve_suggestion(request.user, source)} for source in sources]
            # A convenient default is mapped by the canonical profile grade label,
            # never by equating internal and provider numeric IDs.
            from apps.academics.provider_catalog import catalog, rows, allowed_grades
            allowed = allowed_grades(request.user)
            grade = request.user.student_profile.grade
            name = grade.name if grade else None
            defaults = [field(row,'gradeid','id','key') for row in rows(catalog('grades'))
                        if str(field(row,'gradeid','id','key')) in allowed and field(row,'gradename','name','title') == name]
            return Response({'schema_version':1, 'sources':results,
                             'default_grade_id':defaults[0] if len(defaults)==1 else None})
        except Quiz24Error as error:
            return Response(quiz24_error_data(error), status=error.api_status)


class ReportTopicPreviewView(APIView):
    permission_classes = (IsStudent,)

    def post(self, request):
        serializer = getattr(self, "input_serializer", ReportPreviewInput)(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            strict_scope = None
            if getattr(self, 'strict_report_scope', False):
                from .services.report_topics import confirmed_report_scope
                strict_scope = confirmed_report_scope(request.user, data)
            sources = source_snapshots(request.user, data['sources'])
            authorize_provider(request.user, data['grade_id'], data['course_id'])
            index, labels = tree_context(data['grade_id'], data['course_id'], data['consider_used_question'])
            topics, ancestors = normalize_topics(data['topics'], index, labels)
            filters = {key:data[key] for key in ('grade_id','course_id','question_type','consider_used_question')}
            filters.update(ancestors)
            validate_student_selection(request.user, filters)
            availability = normalize_inventory(Quiz24Service().question_count(**filters))
            # Slow provider responses cannot validate reports/profile that changed in flight.
            source_snapshots(request.user, data['sources'])
            validate_student_selection(request.user, filters)
            if strict_scope is not None:
                strict_scope = confirmed_report_scope(request.user, data)
            warnings = ['موجودی پیشنهادی است و رزرو سؤال یا تضمین ساخت آزمون نیست.']
            if not any(availability.values()):
                warnings.append('برای این محدوده سؤال قابل استفاده‌ای موجود نیست؛ محدوده خودکار گسترده نمی‌شود.')
            if any(row['level']=='chapter' for row in topics):
                warnings.append('انتخاب فصل کامل شامل تمام مباحث آن فصل است؛ این گستردگی را بررسی کنید.')
            if any(source['title_is_custom'] for source in sources):
                warnings.append('عنوان دستی ممکن است موضوعات بیشتری داشته باشد؛ فقط محدوده تأییدشده شما لحاظ شده است.')
            if any(isinstance(source['taxonomy_snapshot']['provider_context'], dict)
                   and source['taxonomy_snapshot']['provider_context'].get('course') not in (None, data['course_id'])
                   for source in sources):
                warnings.append('درس تأییدشده با زمینه ذخیره‌شده برخی فعالیت‌ها متفاوت است؛ این انتخاب به‌عنوان اصلاح شما نمایش داده می‌شود.')
            return Response({'schema_version':1, 'course_scoped':True, 'confirmed':True,
                'scope_origin':'student_confirmed',
                'grade_label':field(option('grades',data['grade_id'],'grade'),'gradename','name','title'),
                'course_label':field(option('courses',data['course_id'],'course',grade_id=data['grade_id']),'coursename','name','title'),
                'sources':sources, 'selected_topics':topics, 'provider_filters':filters,
                'source_links':strict_scope['source_links'] if strict_scope is not None else [{'source_ref':source['source_ref'], 'source_revision':source['source_revision'],
                                 'topics':[{'level':topic['level'],'id':topic['id']} for topic in topics]}
                                for source in sources],
                'availability':availability, 'warnings':warnings, 'exam_created':False})
        except Quiz24Error as error:
            return Response(quiz24_error_data(error), status=error.api_status)


class ReportScopedPreviewView(ReportTopicPreviewView):
    from .services.report_topics import ReportScopeInput
    input_serializer = ReportScopeInput
    strict_report_scope = True


class ReportTopicGroupsView(APIView):
    permission_classes = (IsStudent,)

    def get(self, request):
        from .study_sources import StudySourceQuery
        from apps.daily_reports.study_sources import eligible_sources, load_context, extract_sources
        from .services.report_topics import within_report_scope
        query = StudySourceQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        if any(key in request.query_params for key in ('student', 'student_id')):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('فقط گزارش‌های خودتان قابل استفاده هستند.')
        student = getattr(request.user, 'student_profile', None)
        if not student:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('پروفایل دانش‌آموز موجود نیست.')
        data = query.validated_data
        sources = extract_sources(list(load_context(eligible_sources([student], data['start_date'], data['end_date']))))
        groups, unavailable = {}, 0
        try:
            for source in sources:
                suggestion = resolve_suggestion(request.user, source)['suggestion']
                if not suggestion or not suggestion['topics']:
                    unavailable += 1
                    continue
                key = (suggestion['grade_id'], suggestion['course_id'])
                if key not in groups:
                    index, labels = tree_context(*key)
                    groups[key] = {'grade_id':key[0], 'course_id':key[1],
                        'grade_label':suggestion['grade_label'], 'course_label':suggestion['course_label'],
                        'topics':{}, '_index':index, '_labels':labels}
                group = groups[key]
                for level, paths in group['_index'].items():
                    for identifier, path in paths.items():
                        if not within_report_scope(path, suggestion):
                            continue
                        topic = group['topics'].setdefault((level,identifier), {
                            'level':level, 'id':identifier, 'label':group['_labels'][level][identifier],
                            'path':path, 'chapter_label':group['_labels']['chapter'][path['chapter']], 'sources':[]})
                        topic['sources'].append({'report_item_id':source['report_item_id'], 'source_revision':source['source_revision']})
            result = []
            for group in groups.values():
                group.pop('_index'); group.pop('_labels')
                group['topics'] = list(group['topics'].values())
                result.append(group)
            return Response({'schema_version':1, 'groups':result, 'unavailable_sources':unavailable,
                'eligible_sources':len(sources),
                'range':{'period':data['period'], 'start_date':data['start_date'].isoformat(),
                         'end_date':data['end_date'].isoformat()}})
        except Quiz24Error as error:
            return Response(quiz24_error_data(error), status=error.api_status)
