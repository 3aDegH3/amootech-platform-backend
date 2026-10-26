"""Verified, revisioned Quiz24 catalog. Browsing never contacts the provider."""
import time
from django.db import connection, transaction
from django.utils import timezone
from apps.exams.exceptions import Quiz24Error
from apps.exams.services.quiz24 import Quiz24Service
from .models import ProviderCatalogState, ProviderCatalogRevision, ProviderCatalogNode, ProviderCatalogSyncRun


def read_catalog(method, **kwargs):
    if method not in ('education_periods', 'grades', 'courses', 'course_detail'):
        raise ValueError('Unsupported local catalog method')
    query = ProviderCatalogState.objects.filter(pk=1, revision__isnull=False)
    if method == 'course_detail':
        key = f"{int(kwargs.get('grade_id'))}:{int(kwargs.get('course_id'))}"
        result = query.values_list('revision__payload__details__' + key, flat=True).first()
        if result is None:
            if not query.exists():
                raise Quiz24Error(None, 'فهرست محلی آماده نیست؛ مدیر باید دسته‌بندی Quiz24 را به‌روزرسانی کند.', api_status=503)
            raise Quiz24Error(None, 'درس در فهرست محلی فعال نیست؛ پایه و درس را دوباره انتخاب کنید.', api_status=400)
        return result
    result = query.values_list('revision__payload__' + method, flat=True).first()
    if result is None:
        raise Quiz24Error(None, 'فهرست محلی آماده نیست؛ مدیر باید دسته‌بندی Quiz24 را به‌روزرسانی کند.', api_status=503)
    return result


def _fetch(service, method, **kwargs):
    for attempt in range(3):
        try:
            response = getattr(service, method)(**kwargs)
            from .provider_catalog import field, rows
            # These documented endpoints return complete arrays, not a page cursor.
            # Fail closed if the provider changes to pagination rather than publishing a page.
            if field(response, 'next', 'nextpage', 'hasmore') or (field(response, 'total', 'totalcount') is not None and int(field(response, 'total', 'totalcount')) != len(rows(response))):
                raise ValueError('پاسخ صفحه‌بندی‌شده کامل نیست؛ انتشار فهرست متوقف شد.')
            rows(response)
            return response
        except Quiz24Error as error:
            if attempt == 2 or (error.http_status is not None and error.http_status < 500 and error.http_status != 429):
                raise
            time.sleep(2 ** attempt)


def _dataset(service):
    from .provider_catalog import field, rows, valid_provider_id
    from apps.exams.services.catalog import taxonomy_index
    payload = {method: _fetch(service, method) for method in ('education_periods', 'grades', 'courses')}
    payload['details'] = {}
    nodes = []
    known = {}
    sibling_positions = {}
    def add(kind, row, identifier, parent, grade=0, course=0, level=0):
        if not valid_provider_id(identifier):
            raise ValueError('شناسه نامعتبر در فهرست تأمین‌کننده.')
        identifier = int(identifier)
        key = (kind, grade, course, identifier)
        title = field(row, kind + 'name', 'name', 'title', 'educationperiodtitle')
        if key in known or not isinstance(title, str) or not title.strip():
            raise ValueError('شناسه تکراری یا نام نامعتبر در فهرست تأمین‌کننده.')
        known[key] = True
        siblings = (grade, course, parent, kind)
        position = sibling_positions.get(siblings, 0)
        sibling_positions[siblings] = position + 1
        ordering = field(row, 'order', 'ordering')
        ordering = position if ordering is None else int(ordering)
        if ordering < 0:
            raise ValueError('ترتیب فهرست تأمین‌کننده معتبر نیست.')
        # Add availability/provenance to the existing envelope contract; retain
        # the provider's labels, IDs, type and parent fields verbatim.
        row.update(provider='quiz24', providerId=identifier, active=True,
                   level=kind, ordering=ordering, parentKey=parent,
                   providerGradeId=grade or (identifier if kind == 'grade' else None),
                   providerCourseId=course or (identifier if kind == 'course' else None))
        nodes.append(dict(kind=kind, grade_id_external=grade, course_id_external=course, provider_id=identifier,
                          parent_key=parent, title=title, level=level, ordering=ordering, metadata={k:v for k,v in row.items() if k not in ('children','subjects','objects')}))
        return identifier
    periods = set()
    for row in rows(payload['education_periods']):
        periods.add(add('period', row, field(row, 'educationperiodid', 'id', 'key'), ''))
    period_titles = {int(field(row, 'educationperiodid', 'id', 'key')): field(row, 'title', 'name', 'educationperiodtitle') for row in rows(payload['education_periods'])}
    canonical_names = ('اول','دوم','سوم','چهارم','پنجم','ششم','هفتم','هشتم','نهم','دهم','یازدهم','دوازدهم')
    grades = set()
    for row in rows(payload['grades']):
        period = field(row, 'educationperiodid')
        if not valid_provider_id(period) or int(period) not in periods:
            raise ValueError('پایه بدون دوره آموزشی معتبر.')
        declared_period = field(row, 'educationperiodtitle')
        if declared_period is not None and declared_period != period_titles[int(period)]:
            raise ValueError('نام دوره با رابطه پایه ناسازگار است.')
        label = field(row, 'gradename', 'name', 'title')
        matches = [(n, academic_field) for n, name in enumerate(canonical_names, 1)
                   for academic_field in ('', 'ریاضی', 'تجربی', 'انسانی', 'معارف اسلامی')
                   if label == name + (' ' + academic_field if academic_field else '') and (n >= 10 or not academic_field)]
        number, academic_field = matches[0] if len(matches) == 1 else (None, '')
        normalize = lambda text: ''.join(str(text).split())
        expected_period = 'دوره اول ابتدایی' if number and number <= 3 else 'دوره دوم ابتدایی' if number and number <= 6 else 'دوره اول متوسطه' if number and number <= 9 else 'دوره دوم متوسطه'
        if number and normalize(period_titles[int(period)]) != normalize(expected_period):
            raise ValueError('شماره پایه با دوره آموزشی تأییدشده سازگار نیست.')
        row['canonicalGradeNumber'] = number
        row['verifiedFieldName'] = academic_field
        grades.add(add('grade', row, field(row, 'gradeid', 'id', 'key'), f'period:{period}', level=1))
    courses = set()
    payload['source_courses'] = payload['courses']
    payload['quarantine'] = []
    available_courses = []
    for row in rows(payload['source_courses']):
        grade = field(row, 'gradeid')
        if not valid_provider_id(grade):
            raise ValueError('شناسه پایه درس معتبر نیست.')
        if int(grade) not in grades:
            payload['quarantine'].append({'reason':'MISSING_PROVIDER_GRADE','course':row})
            continue
        available_courses.append(row)
        grade = int(grade)
        course = add('course', row, field(row, 'courseid', 'id', 'key'), f'grade:{grade}', grade=grade, level=2)
        courses.add((grade, course))
        detail = _fetch(service, 'course_detail', grade_id=grade, course_id=course)
        index = taxonomy_index(detail, course, grade)
        payload['details'][f'{grade}:{course}'] = detail
        raw = field(detail, 'result', 'data')
        normalized = isinstance(raw, dict)
        raw = field(raw, 'chapters') if normalized else raw
        if raw and str(field(raw[0], 'type')) == '1':
            raw = field(raw[0], 'children')
        def visit(entries, depth, parent):
            kind = ('chapter', 'subject', 'object')[depth]
            for entry in entries:
                identifier = field(entry, kind + 'id', 'id', 'key')
                if int(identifier) not in index[kind]:
                    raise ValueError('مسیر مبحث تأیید نشد.')
                identifier = add(kind, entry, identifier, parent, grade, course, depth + 3)
                children = field(entry, ('subjects' if depth == 0 else 'objects')) if normalized else field(entry, 'children')
                if depth < 2:
                    visit(children or [], depth + 1, f'{kind}:{identifier}')
        visit(raw, 0, f'course:{course}')
    envelope_key = next(key for key in payload['courses'] if key.lower() in ('result','data'))
    payload['courses'] = {**payload['courses'],envelope_key:available_courses}
    if not periods or not grades or not courses:
        raise ValueError('فهرست خالی قابل انتشار نیست.')
    def strip_counts(value):
        if isinstance(value, dict):
            return {key:strip_counts(entry) for key,entry in value.items() if not key.lower().endswith('count')}
        if isinstance(value, list):
            return [strip_counts(entry) for entry in value]
        return value
    payload['details'] = strip_counts(payload['details'])
    for node in nodes:
        node['metadata'] = strip_counts(node['metadata'])
    return payload, nodes


def sync_catalog(*, allow_removals=False, due_only=False):
    """One DB lock covers fetching, verification and atomic publication.

    Any disappearance requires explicit operator review (--allow-removals), so a
    silently truncated provider response can never replace a known-good revision.
    """
    ProviderCatalogState.objects.get_or_create(pk=1)
    started = timezone.now()
    acquired = False
    try:
        with transaction.atomic():
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [240024])
                    if not cursor.fetchone()[0]:
                        raise RuntimeError('همگام‌سازی دیگری در حال اجراست.')
            state = ProviderCatalogState.objects.select_for_update(nowait=True).get(pk=1)
            acquired = True
            current_month = timezone.localtime(started)
            last_month = timezone.localtime(state.last_success) if state.last_success else None
            if due_only and not state.requested and last_month and (last_month.year, last_month.month) == (current_month.year, current_month.month):
                return state
            # The advisory lock exposes RUNNING without blocking catalog readers.
            payload, incoming = _dataset(Quiz24Service())
            keys = {(n['kind'], n['grade_id_external'], n['course_id_external'], n['provider_id']) for n in incoming}
            previous = set(ProviderCatalogNode.objects.filter(active=True).values_list('kind', 'grade_id_external', 'course_id_external', 'provider_id'))
            if previous - keys and not allow_removals:
                raise ValueError('شناسه‌هایی از فهرست حذف شده‌اند؛ بررسی مدیر و اجرای --allow-removals لازم است.')
            revision = ProviderCatalogRevision.objects.create(payload=payload, started_at=started, counts={**{kind: sum(n['kind'] == kind for n in incoming) for kind in ('period', 'grade', 'course', 'chapter', 'subject', 'object')},'quarantined_courses':len(payload.get('quarantine',[]))})
            ProviderCatalogNode.objects.filter(active=True).update(active=False)
            published = {}
            for values in incoming:
                values = dict(values)
                identity = {key: values.pop(key) for key in ('kind', 'grade_id_external', 'course_id_external', 'provider_id')}
                parent_kind, _, parent_identifier = values['parent_key'].partition(':')
                parent_identity = None
                if parent_kind:
                    parent_grade = 0 if parent_kind in ('period','grade') else identity['grade_id_external']
                    parent_course = identity['course_id_external'] if parent_kind in ('chapter','subject') else 0
                    parent_identity = (parent_kind, parent_grade, parent_course, int(parent_identifier))
                parent = published.get(parent_identity) if parent_identity else None
                if parent_identity and parent is None:
                    raise ValueError('والد محلی مبحث قابل تأیید نیست.')
                metadata = values['metadata']
                node, _ = ProviderCatalogNode.objects.update_or_create(**identity, defaults={**values, 'parent': parent,
                    'canonical_grade_number': metadata.get('canonicalGradeNumber'), 'academic_field': metadata.get('verifiedFieldName', ''),
                    'active': True, 'verified_at': started, 'revision': revision})
                published[(identity['kind'], identity['grade_id_external'], identity['course_id_external'], identity['provider_id'])] = node
            state.revision = revision
            state.last_success = timezone.now()
            state.last_started = started
            state.requested = False
            state.status = 'SUCCESS'
            state.error = ''
            state.save()
            ProviderCatalogSyncRun.objects.create(started_at=started, finished_at=state.last_success,
                                                 status='SUCCESS', revision=revision, counts=revision.counts)
            return state
    except Exception as error:
        if acquired:
            ProviderCatalogSyncRun.objects.create(started_at=started,finished_at=timezone.now(),status='FAILED',
                                                 error='فهرست قبلی حفظ شد؛ گزارش وضعیت را بررسی کنید.')
            ProviderCatalogState.objects.filter(pk=1).update(status='FAILED', requested=False, last_started=started, error='همگام‌سازی ناموفق بود؛ فهرست قبلی حفظ شد. ' + (str(error)[:500] if isinstance(error, ValueError) else 'اتصال، ساختار پاسخ یا قفل پایگاه داده را بررسی کنید.'))
        raise
