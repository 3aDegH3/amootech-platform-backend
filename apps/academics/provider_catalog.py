"""Cached read-only Quiz24 adapter. No internal/provider ID equivalence."""
import hashlib
import json
import logging
from collections import Counter
from django.core.cache import cache
from rest_framework.exceptions import ValidationError, PermissionDenied
from apps.exams.services.quiz24 import Quiz24Service
from apps.accounts.models import User
from .policy import student_period
from apps.exams.exceptions import Quiz24Error

logger = logging.getLogger(__name__)


def field(row, *names):
    normalized = {k.replace('_', '').lower(): v for k, v in row.items()} if isinstance(row, dict) else {}
    return next((normalized[n.lower()] for n in names if n.lower() in normalized), None)


def rows(response):
    value = field(response, 'result', 'data')
    if not isinstance(value, list):
        raise Quiz24Error(None, 'ساختار فهرست آموزشی Quiz24 معتبر نیست.')
    return value


def catalog(method, *, force_refresh=False, **kwargs):
    service = Quiz24Service()
    identity = (service.base_url, service.user_id, service.api_key, method, kwargs)
    key = 'planning:catalog:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    value = cache.get(key)
    if value is None or force_refresh:
        value = getattr(service, method)(**kwargs)
        cache.set(key, value, 900)
    return value


def valid_provider_id(value):
    return type(value) is int and value > 0 or isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0


def option(method, identifier, kind, grade_id=None):
    # courseId is only unique WITHIN a provider grade (including its field).
    for refresh in (False, True):
        response = catalog(method, **({'force_refresh': True} if refresh else {}))
        matches = [row for row in rows(response)
                   if valid_provider_id(identifier) and str(field(row, kind + 'id', 'id', 'key')) == str(identifier)
                   and (grade_id is None or str(field(row, 'gradeid')) == str(grade_id))]
        if len(matches) == 1:
            return matches[0]
    logger.warning('taxonomy rejected level=%s selected_id=%s expected_grade=%s source=Quiz24/%s refreshed=true matches=%s',kind,identifier,grade_id,method,len(matches))
    raise ValidationError({'taxonomy': 'شناسه انتخاب‌شده در فهرست موجود نیست یا مبهم است؛ فهرست را تازه و محدوده را دوباره انتخاب کنید.'})


def grade_access(user):
    from .models import Grade
    student = getattr(user, 'student_profile', None)
    low, high = student_period(student)
    if low == 10 and not student.field_id:
        raise PermissionDenied('رشته تحصیلی دانش‌آموز مشخص نشده است.')
    names = set(Grade.objects.filter(number__range=(low, high)).values_list('name', flat=True))
    provider_grades = rows(catalog('grades'))
    identifiers = Counter(str(field(row, 'gradeid', 'id', 'key')) for row in provider_grades)
    allowed = {}
    for row in provider_grades:
        identifier = field(row, 'gradeid', 'id', 'key')
        label = field(row, 'gradename', 'name', 'title')
        if not valid_provider_id(identifier) or identifiers[str(identifier)] != 1:
            continue
        specific = low == 10 and label in {f'{name} {student.field.name}' for name in names}
        if label in names or specific:
            allowed[str(identifier)] = specific
    return allowed


def allowed_grades(user):
    return set(grade_access(user))


def authorize_provider(user, grade_id=None, course_id=None):
    if user.role != User.Role.STUDENT:
        return
    allowed = grade_access(user)
    if grade_id is not None and str(grade_id) not in allowed:
        # Revalidate an ID against a fresh authoritative catalog before rejecting it.
        catalog('grades', force_refresh=True)
        allowed = grade_access(user)
        if str(grade_id) not in allowed:
            raise PermissionDenied('پایه انتخاب‌شده خارج از دوره یا رشته تحصیلی شماست.')
    if course_id is not None:
        if grade_id is not None:
            candidates = [option('courses', course_id, 'course', grade_id=grade_id)]
        else:
            candidates = [row for row in rows(catalog('courses'))
                          if str(field(row, 'courseid', 'id', 'key')) == str(course_id)
                          and str(field(row, 'gradeid')) in allowed]
        student = user.student_profile
        for row in candidates:
            grade = str(field(row, 'gradeid'))
            if grade not in allowed:
                continue
            if student_period(student)[0] == 10:
                declared_field = field(row, 'fieldname')
                if declared_field is not None and declared_field != student.field.name:
                    continue
                if declared_field is None and not allowed[grade]:
                    continue
            return
        raise PermissionDenied('درس خارج از دوره یا رشته تحصیلی شماست یا رشته آن قابل تأیید نیست.')


def resolve_selection(value):
    if not isinstance(value, dict):
        raise ValidationError({'taxonomy': 'Must be an object.'})
    if not value:
        return {}
    if value.get('provider') != 'quiz24':
        raise ValidationError({'taxonomy': 'Provider must be quiz24; use existing fields for internal IDs.'})
    ids = {k: value[k] for k in ('grade', 'course', 'chapter', 'subject', 'object') if value.get(k) is not None}
    if not ids or any(type(v) is not int or v < 1 for v in ids.values()):
        raise ValidationError({'taxonomy': 'Positive integer IDs are required.'})
    labels = {}
    grade = option('grades', ids['grade'], 'grade') if 'grade' in ids else None
    if grade:
        labels['grade'] = field(grade, 'gradename', 'name', 'title')
    if 'course' in ids:
        course = option('courses', ids['course'], 'course', grade_id=ids.get('grade'))
        if 'grade' in ids and str(field(course, 'gradeid')) != str(ids['grade']):
            raise ValidationError({'taxonomy': 'Course does not belong to grade.'})
        labels['course'] = field(course, 'coursename', 'name', 'title')
    if any(k in ids for k in ('chapter', 'subject', 'object')):
        if 'course' not in ids:
            raise ValidationError({'taxonomy': 'Course is needed to verify provider ancestry.'})
        grade_id = ids.get('grade') or field(course, 'gradeid')
        if not str(grade_id).isascii() or not str(grade_id).isdecimal() or int(grade_id) < 1:
            raise ValidationError({'taxonomy': 'Course grade metadata is unavailable.'})
        from apps.exams.services.report_topics import tree_context
        index, tree_labels = tree_context(int(grade_id), ids['course'])
        deepest = next(k for k in ('object', 'subject', 'chapter') if k in ids)
        path = index[deepest].get(ids[deepest])
        if not path or any(path.get(k) != v for k, v in ids.items() if k != 'grade'):
            raise ValidationError({'taxonomy': 'Invalid or unavailable provider hierarchy.'})
        labels.update({k: tree_labels[k][ids[k]] for k in index if k in ids})
    if any(not isinstance(v, str) or not v.strip() for v in labels.values()):
        raise ValidationError({'taxonomy': 'Catalog labels are unavailable.'})
    return {'provider': 'quiz24', **ids, 'labels': labels}
