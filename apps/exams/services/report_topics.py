"""Resolve and validate exact report scope; provider creation stays in the existing service."""
from datetime import date

from rest_framework import serializers
from rest_framework.exceptions import ValidationError, PermissionDenied

from apps.academics.provider_catalog import authorize_provider, catalog, field, option, rows, allowed_grades
from apps.daily_reports.services import report_today
from apps.daily_reports.study_sources import eligible_sources, load_context, extract_sources
from .catalog import taxonomy_index


class SourceReference(serializers.Serializer):
    report_item_id = serializers.IntegerField(min_value=1)
    source_revision = serializers.RegexField(r'^[a-f0-9]{64}$')


class SourceInput(serializers.Serializer):
    sources = SourceReference(many=True, allow_empty=False, max_length=100)

    def to_internal_value(self, data):
        if isinstance(data, dict) and any(key in data for key in ('student', 'student_id', 'counselor', 'counselor_id')):
            raise ValidationError({'detail':'منابع فقط از حساب دانش‌آموز واردشده خوانده می‌شوند.'})
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise ValidationError({'detail':'پارامتر ناشناخته در درخواست وجود دارد.'})
        return super().to_internal_value(data)

    def validate_sources(self, value):
        ids = [row['report_item_id'] for row in value]
        if len(set(ids)) != len(ids):
            raise ValidationError('گزارش تکراری انتخاب شده است.')
        return value


class ConfirmedTopic(serializers.Serializer):
    level = serializers.ChoiceField(choices=('chapter', 'subject', 'object'))
    id = serializers.IntegerField(min_value=1)

    def to_internal_value(self, data):
        if isinstance(data, dict) and set(data) - {'level', 'id'}:
            raise ValidationError({'detail':'فقط سطح و شناسه مبحث را ارسال کنید؛ والدها در سرور تعیین می‌شوند.'})
        return super().to_internal_value(data)


class ReportPreviewInput(SourceInput):
    grade_id = serializers.IntegerField(min_value=1, max_value=2147483647)
    course_id = serializers.IntegerField(min_value=1, max_value=2147483647)
    topics = ConfirmedTopic(many=True, allow_empty=False, max_length=200)
    confirmed = serializers.BooleanField()
    question_type = serializers.ChoiceField(choices=(1, 2))
    consider_used_question = serializers.BooleanField(default=False)

    def validate_confirmed(self, value):
        if not value:
            raise ValidationError('محدوده مطالعه‌شده باید صریحاً تأیید شود.')
        return value


class ReportScopeInput(ReportPreviewInput):
    # The date-range builder groups repeated occurrences without dropping provenance.
    sources = SourceReference(many=True, allow_empty=False, max_length=2000)


def source_snapshots(user, references):
    user._state.fields_cache.pop('student_profile', None)
    student = getattr(user, 'student_profile', None)
    if not student:
        raise PermissionDenied('پروفایل دانش‌آموز موجود نیست.')
    ids = [row['report_item_id'] for row in references]
    rows = eligible_sources([student], date.min, report_today()).filter(pk__in=ids)
    snapshots = extract_sources(list(load_context(rows)))
    found = {row['report_item_id']: row for row in snapshots}
    # The same response covers missing, foreign, removed and no-longer-eligible rows.
    if set(found) != set(ids):
        raise ValidationError({'sources': 'برخی گزارش‌ها دیگر قابل استفاده نیستند؛ فهرست را دوباره دریافت کنید.'})
    if any(found[row['report_item_id']]['source_revision'] != row['source_revision'] for row in references):
        raise ValidationError({'sources': 'گزارش یا زمینه آموزشی تغییر کرده است؛ دوباره انتخاب و تأیید کنید.'})
    return [found[identifier] for identifier in ids]


def tree_context(grade_id, course_id, consider_used_question=False):
    tree = catalog('course_detail', grade_id=grade_id, course_id=course_id,
                   consider_used_question=consider_used_question)
    index = taxonomy_index(tree, course_id, grade_id)
    labels = {level: {} for level in index}
    value = field(tree, 'result', 'data')
    for container in (tree, value):
        for key, expected in (('gradeid', grade_id), ('courseid', course_id)):
            declared = field(container, key)
            if declared is not None and str(declared) != str(expected):
                raise ValidationError({'taxonomy':'پایه یا درس فهرست مباحث با انتخاب سازگار نیست.'})
    normalized = isinstance(value, dict)
    nodes = field(value, 'chapters') if normalized else value

    def visit(rows, level=2):
        for row in rows:
            for key, expected in (('gradeid', grade_id), ('courseid', course_id)):
                declared = field(row, key)
                if declared is not None and str(declared) != str(expected):
                    raise ValidationError({'taxonomy':'مبحث متعلق به پایه یا درس انتخاب‌شده نیست.'})
            kind = {2:'chapter', 3:'subject', 4:'object'}.get(level if normalized else field(row, 'type'))
            if kind:
                identifier = field(row, kind + 'id', 'id', 'key')
                label = field(row, kind + 'name', 'name', 'title')
                if not isinstance(label, str) or not label.strip():
                    raise ValidationError({'taxonomy': 'نام مبحث در فهرست معتبر نیست.'})
                labels[kind][int(identifier)] = label
            children = field(row, 'subjects' if kind == 'chapter' else 'objects') if normalized else field(row, 'children')
            if children:
                visit(children, level + 1)
    visit(nodes)
    return index, labels


def normalize_topics(topics, index, labels):
    keys = [(row['level'], row['id']) for row in topics]
    if len(keys) != len(set(keys)):
        raise ValidationError({'topics': 'مبحث تکراری انتخاب شده است.'})
    selected = []
    for level, identifier in keys:
        path = index[level].get(identifier)
        if path is None:
            raise ValidationError({'topics': 'مبحث حذف شده یا متعلق به این درس نیست.'})
        selected.append({'level': level, 'id': identifier, 'label': labels[level][identifier], 'path': path})
    # Explicit whole-parent coverage subsumes descendants. Never sum their counts.
    selected = [row for row in selected if not any(
        other['level'] != row['level'] and row['path'].get(other['level']) == other['id']
        for other in selected
    )]
    if len({row['level'] for row in selected}) > 1:
        raise ValidationError({'topics': 'برای شمارش دقیق، مباحث مستقل را از یک سطح انتخاب کنید؛ فصل کامل و زیرمبحث فصل دیگر را جداگانه بررسی کنید.'})
    filters = {level + '_ids': sorted({row['path'][level] for row in selected if level in row['path']})
               for level in ('chapter', 'subject', 'object')}
    return selected, filters


def resolve_internal_suggestion(user, snapshot):
    """Match a unique complete labelled path; never pass internal IDs to Quiz24."""
    selection = snapshot.get('suggested_selection')
    if not selection or selection.get('namespace') != 'academics':
        return None
    hierarchy = selection['hierarchy']
    allowed = allowed_grades(user)
    grade_name = hierarchy['grade']['label']
    field_name = hierarchy['field']['label']
    grades = [row for row in rows(catalog('grades'))
              if str(field(row, 'gradeid', 'id', 'key')) in allowed
              and field(row, 'gradename', 'name', 'title') in (grade_name, f'{grade_name} {field_name}')]
    matches = []
    for grade in grades:
        grade_id = int(field(grade, 'gradeid', 'id', 'key'))
        for course in rows(catalog('courses')):
            if (str(field(course, 'gradeid')) != str(grade_id)
                    or field(course, 'coursename', 'name', 'title') != hierarchy['course']['label']):
                continue
            course_id = int(field(course, 'courseid', 'id', 'key'))
            try:
                authorize_provider(user, grade_id, course_id)
                index, labels = tree_context(grade_id, course_id)
            except (ValidationError, PermissionDenied, ValueError, TypeError):
                continue
            # A course-only legacy declaration cannot establish studied chapters.
            if 'chapter' not in hierarchy:
                continue
            levels = ('subject', 'object') if 'topic' in hierarchy else ('chapter',)
            for level in levels:
                for identifier, path in index[level].items():
                    if labels['chapter'][path['chapter']] != hierarchy['chapter']['label']:
                        continue
                    if 'topic' in hierarchy and labels[level][identifier] != hierarchy['topic']['label']:
                        continue
                    topics, _ = normalize_topics([{'level':level, 'id':identifier}], index, labels)
                    matches.append({'grade_id':grade_id, 'course_id':course_id,
                        'grade_label':field(grade, 'gradename', 'name', 'title'),
                        'course_label':field(course, 'coursename', 'name', 'title'), 'topics':topics})
    return matches[0] if len(matches) == 1 else None


def resolve_suggestion(user, snapshot):
    stored = snapshot['taxonomy_snapshot']['provider_context']
    internal = snapshot['taxonomy_snapshot']['internal']
    if not stored and any(internal.values()):
        suggestion = resolve_internal_suggestion(user, snapshot)
        return {'state':'SUGGESTED' if suggestion else 'NEEDS_SELECTION',
                'reason':None if suggestion else 'مسیر داخلی گزارش تطبیق یکتای معتبر در Quiz24 ندارد.',
                'suggestion':suggestion}
    if not isinstance(stored, dict) or stored.get('provider') != 'quiz24' or any(internal.values()):
        return {'state':'NEEDS_SELECTION', 'reason':'انتخاب داخلی یا متنی به شناسه Quiz24 نگاشت نمی‌شود.', 'suggestion':None}
    course_id = stored.get('course')
    if type(course_id) is not int or course_id < 1:
        return {'state':'NEEDS_SELECTION', 'reason':'درس ذخیره‌شده قابل تأیید نیست.', 'suggestion':None}
    try:
        course = option('courses', course_id, 'course', grade_id=stored.get('grade'))
        grade_id = int(field(course, 'gradeid'))
        if 'grade' in stored and stored['grade'] != grade_id:
            raise ValidationError('پایه ذخیره‌شده با درس سازگار نیست.')
        authorize_provider(user, grade_id, course_id)
        index, labels = tree_context(grade_id, course_id)
        chosen = {level: stored[level] for level in index if level in stored}
        if any(type(value) is not int or value < 1 for value in chosen.values()):
            raise ValidationError('شناسه ذخیره‌شده معتبر نیست.')
        deepest = next((level for level in ('object','subject','chapter') if level in chosen), None)
        topics = []
        if deepest:
            path = index[deepest].get(chosen[deepest])
            if not path or any(path.get(level) != value for level, value in chosen.items()):
                raise ValidationError('مسیر ذخیره‌شده با فهرست کنونی سازگار نیست.')
            topics, _ = normalize_topics([{'level':deepest, 'id':chosen[deepest]}], index, labels)
        return {'state':'SUGGESTED', 'reason':None, 'suggestion':{
            'grade_id':grade_id, 'course_id':course_id,
            'grade_label':field(option('grades', grade_id, 'grade'), 'gradename','name','title'),
            'course_label':field(course,'coursename','name','title'), 'topics':topics,
        }}
    except (ValidationError, PermissionDenied, ValueError, TypeError):
        return {'state':'NEEDS_SELECTION', 'reason':'مسیر ذخیره‌شده قدیمی، نامعتبر یا خارج از دسترسی است؛ از فهرست مجاز انتخاب کنید.', 'suggestion':None}


def within_report_scope(topic_path, suggestion):
    """A chapter declaration permits explicit children, never an implicit expansion."""
    for topic in suggestion['topics']:
        if all(topic_path.get(level) == identifier for level, identifier in topic['path'].items()):
            return True
    return False


def confirmed_report_scope(user, data):
    sources = source_snapshots(user, data['sources'])
    authorize_provider(user, data['grade_id'], data['course_id'])
    index, labels = tree_context(data['grade_id'], data['course_id'], data['consider_used_question'])
    topics, filters = normalize_topics(data['topics'], index, labels)
    suggestions = []
    for source in sources:
        suggestion = resolve_suggestion(user, source)['suggestion']
        if (not suggestion or suggestion['grade_id'] != data['grade_id']
                or suggestion['course_id'] != data['course_id'] or not suggestion['topics']):
            raise ValidationError({'sources': 'زمینه گزارش معتبر نیست؛ مباحث را دوباره دریافت کنید.'})
        suggestions.append(suggestion)
    links = []
    for source, suggestion in zip(sources, suggestions):
        covered = [{'level':t['level'], 'id':t['id']} for t in topics
                   if within_report_scope(t['path'], suggestion)]
        if not covered:
            raise ValidationError({'topics': 'مبحث انتخاب‌شده خارج از محدوده گزارش است.'})
        links.append({'report_item_id':source['report_item_id'], 'source_revision':source['source_revision'], 'topics':covered})
    if any(not any(within_report_scope(topic['path'], suggestion) for suggestion in suggestions) for topic in topics):
        raise ValidationError({'topics': 'مبحث انتخاب‌شده خارج از محدوده گزارش است.'})
    return {'schema_version':1, 'confirmed':True, 'sources':sources, 'source_links':links,
            'selected_topics':topics, 'provider_filters':{
                **{key:data[key] for key in ('grade_id','course_id','question_type','consider_used_question')}, **filters}}
