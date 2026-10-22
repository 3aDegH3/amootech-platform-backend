"""Student exam scope validation using the shared canonical period policy and cache."""
import logging
from rest_framework.exceptions import ValidationError
from apps.accounts.models import User
from apps.academics.provider_catalog import authorize_provider, catalog, field, valid_provider_id


logger = logging.getLogger(__name__)

def taxonomy_index(response, course_id, grade_id=None):
    value = field(response, 'result', 'data')
    normalized = isinstance(value, dict) and isinstance(field(value, 'chapters'), list)
    nodes = field(value, 'chapters') if normalized else value
    if not isinstance(nodes, list):
        raise ValidationError({'taxonomy': 'ساختار فهرست مباحث معتبر نیست؛ دوباره تلاش کنید.'})
    for container in (response, value):
        for key, expected in (('courseid',course_id),('gradeid',grade_id)):
            declared = field(container,key)
            if expected is not None and declared is not None and str(declared) != str(expected):
                raise ValidationError({'taxonomy':'پایه یا درس فهرست با انتخاب سازگار نیست.'})
    index = {'chapter': {}, 'subject': {}, 'object': {}}
    kinds = {2: 'chapter', 3: 'subject', 4: 'object'}

    def visit(rows, level, path):
        if not isinstance(rows, list):
            raise ValidationError({'taxonomy': 'اطلاعات فرزندان مبحث کامل نیست.'})
        for row in rows:
            kind = kinds[level]
            identifier = field(row, kind + 'id', 'id', 'key')
            identifier = int(identifier) if valid_provider_id(identifier) else None
            parent = field(row, 'parentid')
            expected_parent = path[('course', 'chapter', 'subject')[level - 2]]
            if (type(identifier) is not int or identifier < 1
                    or (not normalized and str(field(row, 'type')) != str(level))
                    or (parent is not None and str(parent) != str(expected_parent))
                    or identifier in index[kind]):
                raise ValidationError({'taxonomy': 'شناسه یا مسیر مبحث معتبر نیست.'})
            for ancestor, ancestor_id in path.items():
                declared = field(row, ancestor + 'id')
                if declared is not None and str(declared) != str(ancestor_id):
                    raise ValidationError({'taxonomy': 'مبحث با مسیر والدهای خود سازگار نیست.'})
            label = field(row,kind+'name','name','title')
            declared_grade = field(row,'gradeid')
            if not isinstance(label,str) or not label.strip() or (grade_id is not None and declared_grade is not None and str(declared_grade) != str(grade_id)):
                raise ValidationError({'taxonomy':'نام یا پایه مبحث معتبر نیست.'})
            current = {**path, kind: identifier}
            index[kind][identifier] = current
            children = field(row, ('subjects' if level == 2 else 'objects')) if normalized else field(row, 'children')
            if level < 4:
                visit(children if children is not None else [], level + 1, current)
            elif children:
                raise ValidationError({'taxonomy': 'ساختار زیرمبحث معتبر نیست.'})

    if not normalized:
        roots = [row for row in nodes if str(field(row, 'type')) == '1']
        if roots:
            if len(roots) != 1 or str(field(roots[0], 'key')) != str(course_id) or len(nodes) != 1:
                raise ValidationError({'course_id': 'فهرست مباحث متعلق به درس انتخاب‌شده نیست.'})
            nodes = field(roots[0], 'children')
    visit(nodes, 2, {'course': course_id})
    return index


def validate_student_selection(user, filters):
    """Never mutate/sanitize submitted IDs: reject stale or forged scopes intact."""
    if user.role != User.Role.STUDENT:
        return
    # A count request may have taken long enough for the profile to change.
    user._state.fields_cache.pop('student_profile', None)
    grade_id, course_id = filters['grade_id'], filters['course_id']
    authorize_provider(user, grade_id, course_id)
    selected = {level: filters.get(level + '_ids', []) for level in ('chapter', 'subject', 'object')}
    if not any(selected.values()):
        return
    def validate_tree(tree):
        index = taxonomy_index(tree, course_id, grade_id)
        for level, ids in selected.items():
            if len(ids) != len(set(ids)):
                raise ValidationError({level + '_ids': 'انتخاب تکراری معتبر نیست.'})
            for identifier in ids:
                path = index[level].get(identifier)
                if path is None:
                    logger.warning('taxonomy missing level=%s selected_id=%s expected_course=%s source=Quiz24/courseDetail', level, identifier, course_id)
                    raise ValidationError({level + '_ids': 'مبحث انتخاب‌شده در این درس موجود نیست؛ فهرست را تازه و دوباره انتخاب کنید.'})
                for ancestor in ('chapter', 'subject'):
                    if ancestor != level and ancestor in path and path[ancestor] not in selected[ancestor]:
                        logger.warning('taxonomy ancestry rejected level=%s selected_id=%s expected_parent=%s source=Quiz24/courseDetail',level,identifier,path[ancestor])
                        raise ValidationError({level + '_ids': 'مبحث با والدهای انتخاب‌شده سازگار نیست.'})
        deepest = next(level for level in ('object','subject','chapter') if selected[level])
        for ancestor in ('chapter','subject'):
            if ancestor == deepest or ancestor not in index[deepest][selected[deepest][0]]:
                continue
            expected = {index[deepest][identifier][ancestor] for identifier in selected[deepest]}
            if set(selected[ancestor]) != expected:
                raise ValidationError({'taxonomy':'ترکیب محدوده‌های مستقل از سطوح مختلف مجاز نیست؛ مباحث را از یک سطح انتخاب کنید.'})
    kwargs = dict(grade_id=grade_id, course_id=course_id,
                  consider_used_question=filters.get('consider_used_question', False))
    try:
        validate_tree(catalog('course_detail', **kwargs))
    except ValidationError:
        logger.info('taxonomy revalidating grade_id=%s course_id=%s source=Quiz24/courseDetail', grade_id, course_id)
        validate_tree(catalog('course_detail', force_refresh=True, **kwargs))
