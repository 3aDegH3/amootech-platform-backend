from django.db.models.deletion import ProtectedError
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from apps.accounts.models import User
from .models import Grade, Field, Subject, Chapter, Topic
from .serializers import GradeSerializer, FieldSerializer, SubjectSerializer, ChapterSerializer, TopicSerializer


class AdminWriteAuthenticatedRead(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and (
            request.method in ("GET", "HEAD", "OPTIONS") or request.user.role == User.Role.ADMIN
        )


class AcademicViewSet(viewsets.ModelViewSet):
    permission_classes = (AdminWriteAuthenticatedRead,)
    filter_field = None

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response({"detail": "This item is in use. Deactivate it instead."}, status=status.HTTP_409_CONFLICT)

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.filter_field and self.filter_field in self.request.query_params:
            raw = self.request.query_params[self.filter_field]
            if not raw.isdecimal() or int(raw) < 1:
                raise ValidationError({self.filter_field: "Must be a positive integer."})
            queryset = queryset.filter(**{self.filter_field + "_id": int(raw)})
        return queryset


class GradeViewSet(AcademicViewSet):
    queryset = Grade.objects.all()
    serializer_class = GradeSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(is_active=True) if not self.request.user.is_authenticated else queryset


class FieldViewSet(AcademicViewSet):
    queryset = Field.objects.select_related("grade")
    serializer_class = FieldSerializer
    filter_field = "grade"

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(is_active=True, grade__is_active=True) if not self.request.user.is_authenticated else queryset


class SubjectViewSet(AcademicViewSet):
    queryset = Subject.objects.select_related("field")
    serializer_class = SubjectSerializer
    filter_field = "field"


class ChapterViewSet(AcademicViewSet):
    queryset = Chapter.objects.select_related("subject")
    serializer_class = ChapterSerializer
    filter_field = "subject"


class TopicViewSet(AcademicViewSet):
    queryset = Topic.objects.select_related("chapter")
    serializer_class = TopicSerializer
    filter_field = "chapter"


class AcademicTreeView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        # Single request to load relevant taxonomy for a student or field/grade combo.
        # Query params: grade, field, subject. For counselor, we use student's grade/field if provided via ?student=<id>
        from apps.accounts.models import StudentProfile
        grade_id = request.query_params.get("grade")
        field_id = request.query_params.get("field")
        student_id = request.query_params.get("student")
        # Resolve grade/field from student if given
        if student_id and student_id.isdecimal():
            try:
                sp = StudentProfile.objects.select_related("grade", "field").get(pk=int(student_id))
                if sp.grade_id:
                    grade_id = str(sp.grade_id)
                if sp.field_id:
                    field_id = str(sp.field_id)
            except StudentProfile.DoesNotExist:
                pass
        subjects = Subject.objects.select_related("field").filter(is_active=True)
        chapters = Chapter.objects.select_related("subject").filter(is_active=True)
        topics = Topic.objects.select_related("chapter").filter(is_active=True)
        if field_id and field_id.isdecimal():
            subjects = subjects.filter(field_id=int(field_id))
            chapters = chapters.filter(subject__field_id=int(field_id))
            topics = topics.filter(chapter__subject__field_id=int(field_id))
        elif grade_id and grade_id.isdecimal():
            subjects = subjects.filter(field__grade_id=int(grade_id))
            chapters = chapters.filter(subject__field__grade_id=int(grade_id))
            topics = topics.filter(chapter__subject__field__grade_id=int(grade_id))
        else:
            # no filter: return minimal? return grade-filtered subjects only if field missing
            # limit to avoid huge payload: if no grade/field, return empty
            if not grade_id and not field_id:
                return Response({"subjects": [], "chapters": [], "topics": []})
        # optional subject filter to narrow chapters/topics
        subject_id = request.query_params.get("subject")
        if subject_id and subject_id.isdecimal():
            chapters = chapters.filter(subject_id=int(subject_id))
            topics = topics.filter(chapter__subject_id=int(subject_id))
        return Response({
            "subjects": [{"id": s.pk, "name": s.name, "field": s.field_id} for s in subjects.order_by("ordering", "name")],
            "chapters": [{"id": c.pk, "name": c.name, "subject": c.subject_id} for c in chapters.order_by("ordering", "name")],
            "topics": [{"id": t.pk, "name": t.name, "chapter": t.chapter_id} for t in topics.order_by("ordering", "name")],
        })
