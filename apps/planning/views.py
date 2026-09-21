from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.accounts.models import User
from .models import Plan, PlanDay, PlanItem, StudentFixedCommitment
from .serializers import (
    PlanDayDetailSerializer, PlanDaySerializer, PlanDetailSerializer, PlanItemSerializer,
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


def visible_plans(user):
    queryset = Plan.objects.select_related("student__user", "counselor__user")
    if user.role == User.Role.ADMIN:
        return queryset
    if user.role == User.Role.COUNSELOR:
        return queryset.filter(counselor__user=user, student__counselor__user=user)
    if user.role == User.Role.STUDENT:
        return queryset.filter(student__user=user, status=Plan.Status.PUBLISHED)
    return queryset.none()


class PlanViewSet(viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    queryset = Plan.objects.all()

    def get_queryset(self):
        queryset = visible_plans(self.request.user)
        if self.action == "retrieve":
            items = PlanItem.objects.select_related("subject", "chapter", "topic")
            days = PlanDay.objects.prefetch_related(Prefetch("items", queryset=items))
            queryset = queryset.prefetch_related(Prefetch("days", queryset=days))
        return queryset

    def get_serializer_class(self):
        return PlanDetailSerializer if self.action == "retrieve" else PlanSerializer

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


class PlanDayViewSet(viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = PlanDaySerializer
    queryset = PlanDay.objects.all()

    def get_queryset(self):
        queryset = PlanDay.objects.select_related("plan__student", "plan__counselor__user")
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
        candidate = PlanDay(plan=target, date=date)
        try:
            candidate.full_clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
        candidate.save()
        for item in source.items.all():
            copy_item(item, candidate)
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


class PlanItemViewSet(viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = PlanItemSerializer
    queryset = PlanItem.objects.all()

    def get_queryset(self):
        return PlanItem.objects.select_related("plan_day__plan", "subject", "chapter", "topic").filter(
            plan_day__plan__in=visible_plans(self.request.user)
        )


class StudentFixedCommitmentViewSet(viewsets.ModelViewSet):
    permission_classes = (PlanningAccess,)
    serializer_class = StudentFixedCommitmentSerializer
    queryset = StudentFixedCommitment.objects.all()

    def get_queryset(self):
        queryset = StudentFixedCommitment.objects.select_related("student__user", "student__counselor__user")
        user = self.request.user
        if user.role == User.Role.ADMIN:
            return queryset
        if user.role == User.Role.COUNSELOR:
            return queryset.filter(student__counselor__user=user)
        return queryset.filter(student__user=user)
