"""One local scope/recipient policy for availability, creation and assignments."""
from rest_framework.exceptions import PermissionDenied, ValidationError
from apps.accounts.models import User, StudentProfile
from apps.academics.provider_catalog import option, authorize_provider
from .catalog import validate_student_selection
from .report_topics import tree_context


def recipients(actor, student_ids=(), student=None):
    identifiers = list(student_ids)
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError({'student_ids': 'دانش‌آموز تکراری انتخاب شده است.'})
    if actor.role == User.Role.STUDENT:
        if identifiers:
            raise PermissionDenied('فقط حساب خودتان مجاز است.')
        target = StudentProfile.objects.select_related('user', 'grade', 'field').filter(user=actor).first()
        if not target:
            raise PermissionDenied('پروفایل دانش‌آموز موجود نیست.')
        return [target]
    if student is not None and student.pk not in identifiers:
        identifiers.append(student.pk)
    query = StudentProfile.objects.select_related('user', 'grade', 'field')
    if actor.role == User.Role.COUNSELOR:
        query = query.filter(counselor__user=actor)
    found = list(query.filter(pk__in=identifiers).order_by('pk'))
    if len(found) != len(identifiers):
        raise PermissionDenied('برخی دانش‌آموزان در مسئولیت شما نیستند.')
    return found


def validate_scope(actor, filters, student_ids=(), student=None):
    # Check the active publication for EVERY actor, including a counselor who
    # chooses no assignee. Quarantined rows never enter these local envelopes.
    grade, course = filters['grade_id'], filters['course_id']
    targets = recipients(actor, student_ids, student)
    for target in targets:
        authorize_provider(target.user, grade, course)
    option('grades', grade, 'grade')
    option('courses', course, 'course', grade_id=grade)
    # Reuse the exact student tree/mixed-depth validation for counselor scopes.
    if targets:
        validate_student_selection(targets[0].user, filters)
    else:
        if not any(filters.get(level + '_ids') for level in ('chapter', 'subject', 'object')):
            return targets
        index, _ = tree_context(grade, course)
        selected = {level: filters.get(level + '_ids', []) for level in index}
        deepest = next((level for level in ('object', 'subject', 'chapter') if selected[level]), None)
        for level, ids in selected.items():
            if len(ids) != len(set(ids)) or any(identifier not in index[level] for identifier in ids):
                raise ValidationError({'taxonomy': 'شناسه یا محدوده مبحث معتبر نیست.'})
        if deepest:
            for level in ('chapter', 'subject', 'object'):
                expected = {index[deepest][identifier][level] for identifier in selected[deepest] if level in index[deepest][identifier]}
                if set(selected[level]) != expected:
                    raise ValidationError({'taxonomy': 'والدها یا ترکیب سطوح با محدوده انتخاب‌شده سازگار نیست.'})
    return targets


def scope_snapshot(actor, filters, data):
    from apps.academics.provider_catalog import field
    deepest = next((level for level in ('object', 'subject', 'chapter') if filters.get(level + '_ids')), None)
    index, labels = tree_context(filters['grade_id'], filters['course_id']) if deepest else ({}, {})
    topics = [{'level': deepest, 'id': identifier, 'label': labels[deepest][identifier], 'path': index[deepest][identifier]}
              for identifier in filters.get(deepest + '_ids', [])] if deepest else []
    return {'provider': 'quiz24', 'provider_filters': filters,
            'grade_label': field(option('grades', filters['grade_id'], 'grade'), 'gradename', 'name', 'title'),
            'course_label': field(option('courses', filters['course_id'], 'course', grade_id=filters['grade_id']), 'coursename', 'name', 'title'),
            'whole_course': deepest is None, 'selected_topics': topics,
            'settings': {key: data[key] for key in ('title', 'duration_minutes', 'has_negative_score', 'easy_count', 'medium_count', 'hard_count', 'very_hard_count')},
            'student_ids': [target.pk for target in recipients(actor, data.get('student_ids', []), data.get('student'))]}
