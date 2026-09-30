from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.academics.models import Subject

from .permissions import ServiceTokenPermission
from .views_plan import _resolve_student


@api_view(["GET"])
@authentication_classes([])
@permission_classes([ServiceTokenPermission])
def telegram_subjects(request):
    student, err = _resolve_student(request)
    if err:
        return err
    qs = Subject.objects.filter(is_active=True, field__is_active=True, field__grade__is_active=True)
    # Student's grade/field determines applicable subjects. Reuse existing StudentProfile grade/field relations.
    if student.field_id:
        qs = qs.filter(field_id=student.field_id)
    elif student.grade_id:
        qs = qs.filter(field__grade_id=student.grade_id)
    else:
        # No grade/field on student -> no applicable subjects (avoid leaking unrelated taxonomy)
        qs = qs.none()
    subjects = qs.order_by("ordering", "name").values("id", "name")
    return Response({"subjects": list(subjects)})
