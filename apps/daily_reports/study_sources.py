"""Read-only report extraction. Saved scope is a suggestion, never studied coverage."""
from collections import Counter
import hashlib
import json
from types import SimpleNamespace

from django.db.models import BigIntegerField, F, Q, Value
from rest_framework.exceptions import PermissionDenied

from apps.academics.models import Subject
from apps.academics.policy import scope_catalog, student_period
from apps.planning.models import PlanItem
from apps.planning.titles import build_title
from .models import DailyReportItem
from .services import ACADEMIC_KINDS


def eligible_sources(students, start, end):
    return DailyReportItem.objects.filter(
        report__student__in=students, report__date__range=(start, end),
        reported_status__in=(DailyReportItem.ReportedStatus.COMPLETED, DailyReportItem.ReportedStatus.PARTIAL),
    ).filter(
        Q(plan_item__isnull=True, kind__in=ACADEMIC_KINDS)
        | Q(plan_item__kind__in=ACADEMIC_KINDS,
            plan_item__plan_day__plan__student_id=F('report__student_id'),
            plan_item__plan_day__date=F('report__date'),
            plan_item__plan_day__plan__status='PUBLISHED')
    ).order_by('-report__date', '-report_id', 'ordering', 'pk')


def load_context(queryset):
    # Every possible internal FK can independently be the deepest saved node.
    relations = ['report__student__grade', 'report__student__field', 'plan_item__plan_day__plan']
    for prefix in ('', 'plan_item__'):
        for path in ('subject__field__grade', 'chapter__subject__field__grade',
                     'topic__chapter__subject__field__grade'):
            relations.append(prefix + path)
    return queryset.select_related(*relations)


def internal_hierarchy(activity):
    subject, chapter, topic = activity.subject, activity.chapter, activity.topic
    if topic:
        if chapter and chapter.pk != topic.chapter_id:
            return None
        chapter = topic.chapter
    if chapter:
        if subject and subject.pk != chapter.subject_id:
            return None
        subject = chapter.subject
    if not subject:
        return None
    field = subject.field
    hierarchy = {'grade': field.grade, 'field': field, 'course': subject}
    if chapter:
        hierarchy['chapter'] = chapter
    if topic:
        hierarchy['topic'] = topic
    return hierarchy


def extract_sources(rows):
    """Bounded to a page, with one shared permission query and no provider access."""
    activities = [row.plan_item if row.plan_item_id else row for row in rows]
    hierarchies = [internal_hierarchy(activity) for activity in activities]
    # Batch the canonical policy across students in the current page. Tagged UNION
    # preserves each student's scope without a query per student or per report row.
    students = {row.report.student_id: row.report.student for row in rows}
    permission_reasons, scopes = {}, []
    subject_ids = [h['course'].pk for h in hierarchies if h]
    for student in students.values():
        user = SimpleNamespace(is_authenticated=True, role='STUDENT', student_profile=student)
        try:
            student_period(student)
            scopes.append(scope_catalog(Subject.objects.filter(pk__in=subject_ids).order_by(), user)
                          .annotate(scope_student=Value(student.pk, output_field=BigIntegerField()))
                          .values_list('scope_student', 'pk'))
        except PermissionDenied:
            permission_reasons[student.pk] = 'INVALID_STUDENT_ACADEMIC_PROFILE'
    allowed_subjects = set(scopes[0].union(*scopes[1:])) if scopes else set()

    results = []
    for row, activity, hierarchy in zip(rows, activities, hierarchies):
        saved = activity.taxonomy if isinstance(activity, PlanItem) else {}
        has_saved = saved is not None and saved != {}
        has_internal = any((activity.subject_id, activity.chapter_id, activity.topic_id))
        classification = 'STRUCTURED' if has_saved or has_internal else 'FREE_TEXT'
        reasons = []
        suggested = None
        permission_reason = permission_reasons.get(row.report.student_id)
        if permission_reason:
            reasons.append(permission_reason)
        if has_saved and has_internal:
            reasons.append('MIXED_TAXONOMY_NAMESPACES')
        elif has_saved:
            # Stored provider IDs/labels cannot prove current ancestry, grade or field.
            # Do not consult Quiz24 (including the cached adapter) during extraction.
            if not isinstance(saved, dict) or saved.get('provider') != 'quiz24':
                reasons.append('INVALID_SAVED_TAXONOMY')
            else:
                levels = ('grade', 'course', 'chapter', 'subject', 'object')
                ids = {key: saved[key] for key in levels if key in saved}
                labels = saved.get('labels')
                if (not ids or any(type(value) is not int or value < 1 for value in ids.values())
                        or not isinstance(labels, dict)
                        or any(not isinstance(labels.get(key), str) or not labels[key].strip() for key in ids)
                        or any(key in ids for key in ('chapter', 'subject', 'object')) and 'course' not in ids):
                    reasons.append('INVALID_SAVED_TAXONOMY')
                else:
                    reasons.append('PROVIDER_CONTEXT_REQUIRES_REVALIDATION')
        elif has_internal:
            if not hierarchy or any(not node.is_active for node in hierarchy.values()):
                reasons.append('INVALID_INTERNAL_HIERARCHY')
            elif (row.report.student_id, hierarchy['course'].pk) not in allowed_subjects:
                reasons.append('OUTSIDE_STUDENT_ACADEMIC_SCOPE')
            else:
                suggested = {
                    'namespace': 'academics',
                    'hierarchy': {key: {'id': node.pk, 'label': node.name} for key, node in hierarchy.items()},
                    'deepest_level': next(reversed(hierarchy)),
                }
        else:
            reasons.append('NO_STRUCTURED_CONTEXT')
        custom = activity.title_is_custom if isinstance(activity, PlanItem) else bool(activity.title)
        if classification == 'STRUCTURED' and custom:
            reasons.append('CUSTOM_TITLE_MAY_DESCRIBE_ADDITIONAL_TOPICS')
        state = ('NEEDS_SELECTION' if classification == 'FREE_TEXT'
                 else 'NEEDS_CONFIRMATION' if reasons else 'SUGGESTED')
        labels = {key: node['label'] for key, node in suggested['hierarchy'].items()} if suggested else {}
        title = activity.title or build_title({'labels': labels})
        results.append({
            'source_ref': f'daily-report-item:{row.pk}', 'report_item_id': row.pk,
            'report_id': row.report_id, 'report_date': row.report.date.isoformat(),
            'student_id': row.report.student_id, 'reported_status': row.reported_status,
            'kind': activity.kind, 'title': title, 'title_is_custom': custom,
            'plan_item_id': row.plan_item_id,
            'plan_id': activity.plan_day.plan_id if row.plan_item_id else None,
            'source': row.source, 'classification': classification, 'state': state,
            'taxonomy_snapshot': {'provider_context': saved,
                                  'internal': {'subject': activity.subject_id, 'chapter': activity.chapter_id, 'topic': activity.topic_id}},
            'suggested_selection': suggested, 'academic_context_usable': suggested is not None,
            'needs_taxonomy_selection': suggested is None,
            'requires_topic_confirmation': True, 'reasons': reasons,
        })
    for result in results:
        result['source_revision'] = hashlib.sha256(json.dumps(
            result, sort_keys=True, ensure_ascii=False, default=str,
        ).encode()).hexdigest()
    return results


def page_context_counts(results):
    return {
        'by_classification': dict(Counter(row['classification'] for row in results)),
        'by_state': dict(Counter(row['state'] for row in results)),
        'unresolved_academic_context': sum(not row['academic_context_usable'] for row in results),
    }
