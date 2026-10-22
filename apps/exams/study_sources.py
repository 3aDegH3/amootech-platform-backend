"""Small read-only API for the report extraction service."""
from datetime import timedelta

from django.db.models import Count, Case, When, F, CharField
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import get_object_or_404
from rest_framework.pagination import PageNumberPagination
from rest_framework.views import APIView

from apps.accounts.models import StudentProfile, User
from apps.accounts.permissions import IsAdmin, IsCounselor, IsStudent
from apps.daily_reports.services import report_today
from apps.daily_reports.study_sources import eligible_sources, extract_sources, load_context, page_context_counts


class StudySourceQuery(serializers.Serializer):
    period = serializers.ChoiceField(choices=('today', 'week', 'custom'), default='today')
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    student_id = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        start, end = attrs.get('start_date'), attrs.get('end_date')
        if (start is None) != (end is None):
            raise serializers.ValidationError('تاریخ شروع و پایان را با هم وارد کنید.')
        period = attrs['period']
        if start is not None:
            if 'period' in self.initial_data and period != 'custom':
                raise serializers.ValidationError('بازه سفارشی با دوره انتخاب‌شده سازگار نیست.')
            period = 'custom'
        today = report_today()
        if period == 'custom' and start is None:
            raise serializers.ValidationError('تاریخ شروع و پایان لازم است.')
        if period != 'custom':
            start = today if period == 'today' else today - timedelta(days=(today.weekday() + 2) % 7)
            end = today
        if start > end or end > today or (end - start).days >= 90:
            raise serializers.ValidationError('بازه باید تا امروز، با ترتیب درست و حداکثر ۹۰ روز باشد.')
        return {**attrs, 'period': period, 'start_date': start, 'end_date': end}


class StudySourcePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class StudySourcesView(APIView):
    permission_classes = (IsAdmin | IsCounselor | IsStudent,)

    def get(self, request):
        if request.user.role == User.Role.STUDENT and any(key in request.query_params for key in ('student', 'student_id')):
            raise PermissionDenied('دانش‌آموز فقط می‌تواند گزارش‌های خودش را بخواند.')
        query = StudySourceQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        students = StudentProfile.objects.select_related('user', 'grade', 'field')
        if request.user.role == User.Role.STUDENT:
            student = get_object_or_404(students, user=request.user)
            student_scope = students.filter(pk=student.pk)
        else:
            if request.user.role == User.Role.COUNSELOR:
                students = students.filter(counselor__user=request.user)
            if 'student_id' in data:
                student = get_object_or_404(students, pk=data['student_id'])
                student_scope = students.filter(pk=student.pk)
            elif request.user.role == User.Role.COUNSELOR:
                student, student_scope = None, students
            else:
                raise serializers.ValidationError({'student_id': 'شناسه دانش‌آموز لازم است.'})
        rows = eligible_sources(student_scope, data['start_date'], data['end_date'])
        counts = list(rows.order_by().annotate(activity_kind=Case(
            When(plan_item__isnull=True, then=F('kind')), default=F('plan_item__kind'), output_field=CharField(),
        )).values('reported_status', 'activity_kind').annotate(count=Count('pk')))
        pagination = StudySourcePagination()
        page = pagination.paginate_queryset(load_context(rows), request, view=self)
        results = extract_sources(page)
        by_status = {key: sum(row['count'] for row in counts if row['reported_status'] == key)
                     for key in ('COMPLETED', 'PARTIAL')}
        by_kind = {key: sum(row['count'] for row in counts if row['activity_kind'] == key)
                   for key in ('STUDY', 'TEST', 'REVIEW', 'EXAM')}
        response = pagination.get_paginated_response(results)
        response.data.update({
            'schema_version': 1, 'student_id': student.pk if student else None,
            'range': {'period': data['period'], 'start_date': data['start_date'].isoformat(),
                      'end_date': data['end_date'].isoformat(), 'timezone': 'Asia/Tehran'},
            'summary': {'scope': 'filtered_range', 'by_status': by_status, 'by_kind': by_kind},
            'page_summary': {'scope': 'current_page', **page_context_counts(results)},
        })
        return response
