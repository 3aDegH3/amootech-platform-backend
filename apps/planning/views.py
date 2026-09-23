from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.deletion import ProtectedError
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.accounts.models import User
from apps.accounts.permissions import IsStudent
from .execution import transition
from .models import Plan, PlanDay, PlanItem, PlanItemExecution, StudentFixedCommitment
from .exports import render_excel, render_pdf
from .serializers import (
    FinishExecutionSerializer, PlanDayDetailSerializer, PlanDaySerializer, PlanDetailSerializer,
    PlanItemExecutionSerializer, PlanItemSerializer,
    PlanSerializer, StudentFixedCommitmentSerializer, manageable_plan,
)


class PlanningAccess(BasePermission):
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.user.role == User.Role.ADMIN:
            return True
        if request.user.role == User.Role.COUNSELOR:
            return request.method in ("GET", "HEAD", "OPTIONS") if view.basename == "commitments" else True
        if request.user.role == User.Role.STUDENT:
            return request.method in ("GET", "HEAD", "OPTIONS") if view.basename != "commitments" else True
        return False


class ExecutionReadAccess(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in (User.Role.STUDENT, User.Role.ADMIN)


class ProtectedReportDeleteMixin:
    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        today = timezone.localdate()
        if isinstance(obj, Plan):
            if obj.days.filter(date__lte=today).exists():
                return Response({"detail": "برنامه‌ای که روز گذشته یا امروز دارد قابل حذف نیست."}, status=status.HTTP_409_CONFLICT)
        elif isinstance(obj, PlanDay):
            if obj.date < today:
                return Response({"detail": "روزهای گذشته فقط قابل مشاهده هستند."}, status=status.HTTP_409_CONFLICT)
            if obj.items.filter(execution__isnull=False).exists() or obj.items.filter(report_items__isnull=False).exists():
                return Response({"detail": "این روز دارای عملکرد ثبت‌شده دانش‌آموز است و قابل حذف نیست."}, status=status.HTTP_409_CONFLICT)
        else:
            if obj.plan_day.date < today:
                return Response({"detail": "روزهای گذشته فقط قابل مشاهده هستند."}, status=status.HTTP_409_CONFLICT)
            if hasattr(obj, "execution") or obj.report_items.exists():
                return Response({"detail": "این باکس توسط دانش‌آموز شروع یا ثبت شده و قابل حذف نیست."}, status=status.HTTP_409_CONFLICT)
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response({"detail": "This planned activity is included in a daily report and cannot be deleted."}, status=status.HTTP_409_CONFLICT)


def visible_plans(user):
    queryset = Plan.objects.select_related("student__user", "counselor__user")
    if user.role == User.Role.ADMIN:
        return queryset
    if user.role == User.Role.COUNSELOR:
        return queryset.filter(counselor__user=user, student__counselor__user=user)
    if user.role == User.Role.STUDENT:
        return queryset.filter(student__user=user, status=Plan.Status.PUBLISHED)
    return queryset.none()


class PlanViewSet(ProtectedReportDeleteMixin, viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    queryset = Plan.objects.all()

    def get_queryset(self):
        queryset = visible_plans(self.request.user)
        student = self.request.query_params.get("student")
        if student and student.isdecimal():
            queryset = queryset.filter(student_id=int(student))
        if self.action in ("retrieve", "export_pdf", "export_excel"):
            items = PlanItem.objects.select_related("subject", "chapter", "topic", "execution").prefetch_related("report_items")
            days = PlanDay.objects.prefetch_related(Prefetch("items", queryset=items))
            queryset = queryset.prefetch_related(Prefetch("days", queryset=days))
        return queryset

    def get_serializer_class(self):
        return PlanDetailSerializer if self.action == "retrieve" else PlanSerializer

    @action(detail=True, methods=("get",), permission_classes=(ExecutionReadAccess,))
    def executions(self, request, pk=None):
        plan = self.get_object()
        records = PlanItemExecution.objects.filter(plan_item__plan_day__plan=plan)
        if request.user.role == User.Role.STUDENT:
            records = records.filter(student__user=request.user)
        return Response(PlanItemExecutionSerializer(records, many=True, context={"now": timezone.now()}).data)

    @action(detail=True, methods=("get",), url_path="export/pdf")
    def export_pdf(self, request, pk=None):
        plan = self.get_object()
        response = HttpResponse(render_pdf(plan), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="plan-{plan.pk}.pdf"'
        return response

    @action(detail=True, methods=("get",), url_path="export/excel")
    def export_excel(self, request, pk=None):
        plan = self.get_object()
        response = HttpResponse(
            render_excel(plan),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="plan-{plan.pk}.xlsx"'
        return response

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def publish(self, request, pk=None):
        plan = self.get_object()
        if plan.status != Plan.Status.DRAFT:
            raise serializers.ValidationError({"status": "Plan is already published."})
        days = list(plan.days.prefetch_related("items"))
        if not days or any(not day.items.all() for day in days):
            raise serializers.ValidationError({"days": "A published plan needs at least one day and an item in every day."})
        try:
            plan.full_clean()
            for day in days:
                day.full_clean()
                for item in day.items.all():
                    item.full_clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
        plan.status = Plan.Status.PUBLISHED
        plan.published_at = timezone.now()
        plan.save()
        return Response(PlanSerializer(plan, context=self.get_serializer_context()).data)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def duplicate(self, request, pk=None):
        source = self.get_object()
        copied = Plan.objects.create(
            student=source.student, counselor=source.counselor, title=source.title,
            start_date=source.start_date, end_date=source.end_date,
        )
        for source_day in source.days.prefetch_related("items"):
            day = PlanDay.objects.create(plan=copied, date=source_day.date)
            for item in source_day.items.all():
                copy_item(item, day)
        return Response(PlanSerializer(copied, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)


class PlanDayViewSet(ProtectedReportDeleteMixin, viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = PlanDaySerializer
    queryset = PlanDay.objects.all()

    def get_queryset(self):
        queryset = PlanDay.objects.select_related("plan__student", "plan__counselor__user")
        if self.action in ("retrieve", "duplicate"):
            queryset = queryset.prefetch_related(Prefetch("items", queryset=PlanItem.objects.select_related("subject", "chapter", "topic", "execution").prefetch_related("report_items")))
        return queryset.filter(plan__in=visible_plans(self.request.user))

    def get_serializer_class(self):
        return PlanDayDetailSerializer if self.action == "retrieve" else PlanDaySerializer

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def duplicate(self, request, pk=None):
        source = self.get_object()
        action_input = DuplicateDaySerializer(data=request.data)
        action_input.is_valid(raise_exception=True)
        target = action_input.validated_data.get("target_plan", source.plan)
        if not manageable_plan(target, request.user):
            raise serializers.ValidationError({"target_plan": "This plan is not available to you."})
        date = action_input.validated_data["date"]
        if date < timezone.localdate():
            raise serializers.ValidationError({"date": "نمی‌توان روزی را در گذشته کپی کرد."})
        candidate = PlanDay(plan=target, date=date)
        try:
            candidate.full_clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
        candidate.save()
        for item in source.items.all():
            copy_item(item, candidate)
        candidate = PlanDay.objects.prefetch_related(Prefetch("items", queryset=PlanItem.objects.select_related("subject", "chapter", "topic"))).get(pk=candidate.pk)
        return Response(PlanDayDetailSerializer(candidate, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)


class DuplicateDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    target_plan = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.all(), required=False)


def copy_item(source, target_day):
    return PlanItem.objects.create(
        plan_day=target_day, kind=source.kind, ordering=source.ordering, title=source.title,
        planned_duration_minutes=source.planned_duration_minutes, start_time=source.start_time,
        end_time=source.end_time, note=source.note, subject=source.subject, chapter=source.chapter,
        topic=source.topic, test_count=source.test_count,
    )


class PlanItemViewSet(ProtectedReportDeleteMixin, viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = PlanItemSerializer
    queryset = PlanItem.objects.all()

    def get_queryset(self):
        return PlanItem.objects.select_related("plan_day__plan", "subject", "chapter", "topic", "execution").prefetch_related("report_items").filter(
            plan_day__plan__in=visible_plans(self.request.user)
        )

    def _execute(self, request, pk, action_name, *, complete=None):
        item = self.get_object()
        execution = transition(request.user, item.pk, action_name, complete=complete)
        return Response(PlanItemExecutionSerializer(execution, context={"now": timezone.now()}).data)

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,))
    def start(self, request, pk=None):
        return self._execute(request, pk, "start")

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,))
    def pause(self, request, pk=None):
        return self._execute(request, pk, "pause")

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,))
    def resume(self, request, pk=None):
        return self._execute(request, pk, "resume")

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,))
    def finish(self, request, pk=None):
        payload = FinishExecutionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return self._execute(request, pk, "finish", complete=payload.validated_data["complete"])

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,), url_path="quick-complete")
    def quick_complete(self, request, pk=None):
        return self._execute(request, pk, "quick_complete")

    @action(detail=True, methods=("post",), permission_classes=(IsStudent,), url_path="not-done")
    def not_done(self, request, pk=None):
        return self._execute(request, pk, "mark_not_done")


class StudentFixedCommitmentViewSet(viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = StudentFixedCommitmentSerializer
    queryset = StudentFixedCommitment.objects.all()

    def get_queryset(self):
        queryset = StudentFixedCommitment.objects.select_related("student__user", "student__counselor__user")
        user = self.request.user
        if user.role == User.Role.ADMIN:
            scoped = queryset
        elif user.role == User.Role.COUNSELOR:
            scoped = queryset.filter(student__counselor__user=user)
        else:
            scoped = queryset.filter(student__user=user)
        student = self.request.query_params.get("student")
        if student and student.isdecimal():
            scoped = scoped.filter(student_id=int(student))
        return scoped
