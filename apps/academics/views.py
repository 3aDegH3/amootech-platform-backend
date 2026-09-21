from django.db.models.deletion import ProtectedError
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
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


class FieldViewSet(AcademicViewSet):
    queryset = Field.objects.select_related("grade")
    serializer_class = FieldSerializer
    filter_field = "grade"


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
