from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.accounts.models import CounselorProfile, StudentProfile, User
from .models import Plan, PlanDay, PlanItem, StudentFixedCommitment


def validate_model(serializer, attrs):
    candidate = serializer.instance or serializer.Meta.model()
    for key, value in attrs.items():
        setattr(candidate, key, value)
    try:
        candidate.full_clean()
    except DjangoValidationError as error:
        raise serializers.ValidationError(error.message_dict if hasattr(error, "message_dict") else error.messages) from error
    return attrs


def manageable_plan(plan, user):
    return user.role == User.Role.ADMIN or (
        user.role == User.Role.COUNSELOR
        and plan.counselor.user_id == user.pk
        and plan.student.counselor_id == plan.counselor_id
    )


class PlanItemSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    chapter_name = serializers.CharField(source="chapter.name", read_only=True)
    topic_name = serializers.CharField(source="topic.name", read_only=True)

    class Meta:
        model = PlanItem
        fields = ("id", "plan_day", "kind", "ordering", "title", "planned_duration_minutes", "start_time", "end_time", "note", "subject", "subject_name", "chapter", "chapter_name", "topic", "topic_name", "test_count")

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot edit plan items.")
        day = attrs.get("plan_day", self.instance.plan_day if self.instance else None)
        if day and not manageable_plan(day.plan, user):
            raise serializers.ValidationError({"plan_day": "This plan is not available to you."})
        if not day:
            return attrs
        return validate_model(self, attrs)


class PlanDaySerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanDay
        fields = ("id", "plan", "date")

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot edit plan days.")
        plan = attrs.get("plan", self.instance.plan if self.instance else None)
        if plan and not manageable_plan(plan, user):
            raise serializers.ValidationError({"plan": "This plan is not available to you."})
        if not plan:
            return attrs
        return validate_model(self, attrs)


class PlanDayDetailSerializer(PlanDaySerializer):
    items = PlanItemSerializer(many=True, read_only=True)

    class Meta(PlanDaySerializer.Meta):
        fields = PlanDaySerializer.Meta.fields + ("items",)


class PlanSerializer(serializers.ModelSerializer):
    counselor = serializers.PrimaryKeyRelatedField(queryset=CounselorProfile.objects.all(), required=False)
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all())

    class Meta:
        model = Plan
        fields = ("id", "student", "counselor", "title", "start_date", "end_date", "status", "published_at", "created_at", "updated_at")
        read_only_fields = ("status", "published_at", "created_at", "updated_at")

    def validate(self, attrs):
        if {"status", "published_at"} & set(self.initial_data):
            raise serializers.ValidationError({"status": "Use the publish action to change plan status."})
        user = self.context["request"].user
        if user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot edit plans.")
        if user.role == User.Role.COUNSELOR:
            if "counselor" in self.initial_data:
                raise serializers.ValidationError({"counselor": "Counselor is set from your account."})
            attrs["counselor"] = user.counselor_profile
        student = attrs.get("student", self.instance.student if self.instance else None)
        counselor = attrs.get("counselor", self.instance.counselor if self.instance else None)
        if not counselor:
            raise serializers.ValidationError({"counselor": "This field is required."})
        if student and student.counselor_id != counselor.pk:
            raise serializers.ValidationError({"student": "Student must be assigned to this counselor."})
        start = attrs.get("start_date", self.instance.start_date if self.instance else None)
        end = attrs.get("end_date", self.instance.end_date if self.instance else None)
        if self.instance and start and end and self.instance.days.exclude(date__range=(start, end)).exists():
            raise serializers.ValidationError({"start_date": "Existing plan days must remain within the date range."})
        return validate_model(self, attrs)


class PlanDetailSerializer(PlanSerializer):
    days = PlanDayDetailSerializer(many=True, read_only=True)

    class Meta(PlanSerializer.Meta):
        fields = PlanSerializer.Meta.fields + ("days",)


class StudentFixedCommitmentSerializer(serializers.ModelSerializer):
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all(), required=False)
    class Meta:
        model = StudentFixedCommitment
        fields = ("id", "student", "kind", "title", "weekday", "start_time", "end_time", "active")

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.COUNSELOR:
            raise serializers.ValidationError("Counselors can only view fixed commitments.")
        if user.role == User.Role.STUDENT:
            if "student" in self.initial_data:
                raise serializers.ValidationError({"student": "Student is set from your account."})
            attrs["student"] = user.student_profile
        if "student" not in attrs and not self.instance:
            raise serializers.ValidationError({"student": "This field is required."})
        return validate_model(self, attrs)
