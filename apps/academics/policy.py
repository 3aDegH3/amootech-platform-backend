"""Role-aware catalog scope; grade numbers are canonical metadata, never provider IDs."""
from rest_framework.exceptions import PermissionDenied
from apps.accounts.models import User
from .models import Grade


def student_period(student):
    number = student.grade.number if student and student.grade_id else None
    if number is None or not 1 <= number <= 12:
        raise PermissionDenied('پایه معتبر دانش‌آموز برای دسترسی به فهرست آموزشی مشخص نشده است.')
    if student.field_id and student.field.grade_id != student.grade_id:
        raise PermissionDenied('رشته دانش‌آموز با پایه تحصیلی سازگار نیست.')
    return (1, 6) if number <= 6 else (7, 9) if number <= 9 else (10, 12)


def scope_catalog(queryset, user):
    if not user.is_authenticated or user.role != User.Role.STUDENT:
        return queryset
    student = getattr(user, 'student_profile', None)
    low, high = student_period(student)
    paths = {'grade': '', 'field': 'grade__', 'subject': 'field__grade__',
             'chapter': 'subject__field__grade__', 'topic': 'chapter__subject__field__grade__'}
    path = paths[queryset.model._meta.model_name]
    queryset = queryset.filter(**{path + 'number__range': (low, high)})
    if low == 10 and queryset.model is not Grade:
        if not student.field_id:
            raise PermissionDenied('رشته تحصیلی دانش‌آموز مشخص نشده است.')
        field_path = {'field': '', 'subject': 'field__', 'chapter': 'subject__field__', 'topic': 'chapter__subject__field__'}[queryset.model._meta.model_name]
        queryset = queryset.filter(**{field_path + 'name': student.field.name})
    return queryset
