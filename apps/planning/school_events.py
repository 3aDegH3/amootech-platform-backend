"""School exams are calendar occurrences, independent of study/online exams."""
from datetime import timedelta
from .models import StudentFixedEvent


def occurrences(student_id, start, end):
    sources = list(StudentFixedEvent.objects.filter(student_id=student_id, active=True, kind__in=('EXAM', 'QUIZ')))
    result = []
    day = start
    while day <= end:
        for source in sources:
            if source.date is not None:
                if source.date != day:
                    continue
            elif source.weekday != day.weekday():
                continue
            if (source.valid_from and day < source.valid_from) or (source.valid_until and day > source.valid_until):
                continue
            result.append({'id': source.pk, 'date': day.isoformat(), 'kind': source.kind, 'title': source.title,
                           'start_time': source.start_time.isoformat() if source.start_time else None,
                           'end_time': source.end_time.isoformat() if source.end_time else None,
                           'recurring': source.weekday is not None})
        day += timedelta(days=1)
    return result
