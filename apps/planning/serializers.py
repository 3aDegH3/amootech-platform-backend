from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from django.utils import timezone

from apps.accounts.models import CounselorProfile, StudentProfile, User
from .models import Plan, PlanDay, PlanItem, PlanItemExecution, StudentFixedCommitment


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


def item_has_student_data(item):
    return hasattr(item, "execution") or item.report_items.exists()


def enforce_editable_day(day):
    if day.date < timezone.localdate():
        raise serializers.ValidationError("روزهای گذشته فقط قابل مشاهده هستند.")


class PlanItemSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    chapter_name = serializers.CharField(source="chapter.name", read_only=True)
    topic_name = serializers.CharField(source="topic.name", read_only=True)
    counselor_editable = serializers.SerializerMethodField()
    edit_lock_reason = serializers.SerializerMethodField()

    class Meta:
        model = PlanItem
        fields = ("id", "plan_day", "kind", "ordering", "title", "planned_duration_minutes", "start_time", "end_time", "note", "subject", "subject_name", "chapter", "chapter_name", "topic", "topic_name", "test_count", "counselor_editable", "edit_lock_reason")

    def get_counselor_editable(self, obj):
        return obj.plan_day.date >= timezone.localdate() and not item_has_student_data(obj)

    def get_edit_lock_reason(self, obj):
        if obj.plan_day.date < timezone.localdate():
            return "گذشته — فقط مشاهده"
        if item_has_student_data(obj):
            return "این باکس توسط دانش‌آموز شروع یا ثبت شده و دیگر قابل ویرایش کامل نیست."
        return None

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot edit plan items.")
        day = attrs.get("plan_day", self.instance.plan_day if self.instance else None)
        if day and not manageable_plan(day.plan, user):
            raise serializers.ValidationError({"plan_day": "This plan is not available to you."})
        if not day:
            return attrs
        enforce_editable_day(day)
        if self.instance and item_has_student_data(self.instance):
            raise serializers.ValidationError("این باکس توسط دانش‌آموز شروع یا ثبت شده و دیگر قابل ویرایش کامل نیست.")
        return validate_model(self, attrs)


class PlanItemExecutionSerializer(serializers.ModelSerializer):
    elapsed_seconds = serializers.SerializerMethodField()
    completion_method = serializers.SerializerMethodField()

    class Meta:
        model = PlanItemExecution
        fields = ("plan_item", "status", "started_at", "current_session_started_at", "accumulated_seconds", "elapsed_seconds", "completed_at", "completion_method")

    def get_elapsed_seconds(self, obj):
        return obj.elapsed_seconds(self.context.get("now") or timezone.now())

    def get_completion_method(self, obj):
        return "TIMER" if obj.started_at else "QUICK"


class FinishExecutionSerializer(serializers.Serializer):
    complete = serializers.BooleanField(required=True)


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
        if self.instance:
            enforce_editable_day(self.instance)
        target_date = attrs.get("date", self.instance.date if self.instance else None)
        if target_date and target_date < timezone.localdate():
            raise serializers.ValidationError({"date": "نمی‌توان برای روز گذشته برنامه ایجاد یا جابه‌جا کرد."})
        if self.instance and target_date != self.instance.date and (
            self.instance.items.filter(execution__isnull=False).exists()
            or self.instance.items.filter(report_items__isnull=False).exists()
        ):
            raise serializers.ValidationError({"date": "روز دارای عملکرد ثبت‌شده دانش‌آموز است و تاریخ آن قابل تغییر نیست."})
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
        if self.instance and self.instance.status == Plan.Status.PUBLISHED:
            changed = set(self.initial_data) - {"title"}
            if changed:
                raise serializers.ValidationError("Only the title of a published plan can change; duplicate it for structural changes.")
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
