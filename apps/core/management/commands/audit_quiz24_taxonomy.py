"""Read-only live taxonomy and report-reference audit; never infer provider IDs."""
import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import APIException
from apps.accounts.models import StudentProfile
from apps.academics.policy import student_period
from apps.academics.provider_catalog import catalog, rows, field, resolve_selection, authorize_provider
from apps.daily_reports.models import DailyReportItem
from apps.daily_reports.services import report_today
from apps.planning.models import PlanItem, PlanItemExecution
from apps.exams.exceptions import Quiz24Error
from apps.exams.services.catalog import taxonomy_index
from .seed_demo import STUDENT_PATTERN


class Command(BaseCommand):
    help = 'Read-only live Quiz24 audit with aggregate counts; no data changed.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **options):
        try:
            grades = rows(catalog('grades', force_refresh=True))
            courses = rows(catalog('courses', force_refresh=True))
            if not grades or not courses:
                raise CommandError('Quiz24 catalog is empty; no data changed.')
            return self.audit(grades, courses, options)
        except Quiz24Error as error:
            raise CommandError(f'Quiz24 catalog unavailable; no data changed: {error}') from error

    def audit(self, grades, courses, options):
        summary = {'catalog_available': True, 'plans': Counter(), 'reports': Counter(),
                   'demo_plans': Counter(), 'demo_reports': Counter(), 'statuses': Counter(),
                   'eligible_report_taxonomy': Counter(), 'integrity': Counter()}
        classifications, validated, refreshed = {}, {}, set()
        def classify(item):
            value = item.taxonomy
            if not value:
                return 'missing'
            if not isinstance(value, dict) or value.get('provider') != 'quiz24':
                return 'invalid'
            grade, course = value.get('grade'), value.get('course')
            if any(type(value.get(k)) is not int or value[k] < 1 for k in value if k in ('grade','course','chapter','subject','object')):
                return 'invalid'
            if any((item.subject_id, item.chapter_id, item.topic_id)):
                return 'invalid'
            if any(k not in value for k in ('grade','course','chapter')):
                return 'invalid'
            if (sum(str(field(r,'gradeid','id','key')) == str(grade) for r in grades) > 1
                    or sum(str(field(r,'courseid','id','key')) == str(course) and str(field(r,'gradeid')) == str(grade) for r in courses) > 1):
                return 'ambiguous'
            student = item.plan_day.plan.student
            key = (json.dumps(value, sort_keys=True), student.grade_id, student.field_id)
            if key not in validated:
                try:
                    if (grade, course) not in refreshed:
                        tree = catalog('course_detail', grade_id=grade, course_id=course, force_refresh=True)
                        taxonomy_index(tree, course, grade)
                        refreshed.add((grade, course))
                    selection = resolve_selection(value)
                    authorize_provider(student.user, selection['grade'], selection['course'])
                    validated[key] = 'valid'
                except (APIException, ValueError, TypeError, KeyError):
                    validated[key] = 'invalid'
            return validated[key]

        demo_students = set(StudentProfile.objects.filter(user__email__endswith='@demo.local')
                            .values_list('pk','user__username'))
        demo_ids = {pk for pk, name in demo_students if STUDENT_PATTERN.fullmatch(name)}
        for student in StudentProfile.objects.select_related('grade','field'):
            try:
                low, _ = student_period(student)
                if low == 10 and not student.field_id:
                    summary['integrity']['invalid_student_profile'] += 1
            except APIException:
                summary['integrity']['invalid_student_profile'] += 1
        for item in PlanItem.objects.select_related('plan_day__plan__student__user'):
            day = item.plan_day
            if not day.plan.start_date <= day.date <= day.plan.end_date:
                summary['integrity']['plan_date_outside_range'] += 1
            if item.kind == PlanItem.Kind.EVENT:
                continue
            category = classify(item)
            classifications[item.pk] = category
            for bucket in ('plans', 'demo_plans') if day.plan.student_id in demo_ids else ('plans',):
                summary[bucket][category] += 1
                if any((item.subject_id, item.chapter_id, item.topic_id)):
                    summary[bucket]['legacy'] += 1
        for row in DailyReportItem.objects.select_related('report','plan_item__plan_day__plan'):
            summary['statuses'][row.reported_status or 'UNSET'] += 1
            if row.reported_status is not None and row.reported_status not in DailyReportItem.ReportedStatus.values:
                summary['integrity']['invalid_status'] += 1
            if row.report.date > report_today():
                summary['integrity']['future_report_item'] += 1
            if row.plan_item_id:
                day = row.plan_item.plan_day
                if day.plan.student_id != row.report.student_id or day.date != row.report.date or day.plan.status != 'PUBLISHED':
                    summary['integrity']['broken_report_plan_link'] += 1
            if row.activity_kind == PlanItem.Kind.EVENT:
                continue
            category = classifications.get(row.plan_item_id, 'missing')
            activity = row.plan_item if row.plan_item_id else row
            for bucket in ('reports','demo_reports') if row.report.student_id in demo_ids else ('reports',):
                summary[bucket][category] += 1
                if any((activity.subject_id, activity.chapter_id, activity.topic_id)):
                    summary[bucket]['legacy'] += 1
            if row.reported_status in ('COMPLETED','PARTIAL'):
                summary['eligible_report_taxonomy'][category] += 1
        summary['integrity']['broken_execution_link'] = sum(
            row.student_id != row.plan_item.plan_day.plan.student_id
            for row in PlanItemExecution.objects.select_related('plan_item__plan_day__plan'))
        for name in ('plans','reports','demo_plans','demo_reports'):
            for category in ('valid','missing','invalid','ambiguous','legacy'):
                summary[name].setdefault(category,0)
        self.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        if not options['json']:
            problems = sum(summary['integrity'].values()) + sum(summary[b][c] for b in ('plans','reports') for c in ('missing','invalid','ambiguous'))
            self.stdout.write(f'Audit problems: {problems}; no data changed.')
