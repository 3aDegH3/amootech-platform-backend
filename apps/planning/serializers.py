from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from django.utils import timezone

from apps.accounts.models import CounselorProfile, StudentProfile, User
from .models import Plan, PlanDay, PlanItem, PlanItemExecution, StudentFixedCommitment, StudentFixedEvent, StudentSchoolSlot


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


def manageable_student(student, user):
    """Return True if user may manage fixed commitments for that student."""
    if user.role == User.Role.ADMIN:
        return True
    if user.role == User.Role.COUNSELOR:
        try:
            counselor_id = user.counselor_profile.pk  # type: ignore[attr-defined]
        except Exception:
            return False
        return student.counselor_id == counselor_id
    return False


def item_has_student_data(item):
    return hasattr(item, "execution") or item.report_items.exists()


def enforce_editable_day(day):
    # All PlanDays are always active regardless of date (per product rule).
    # Only execution/report protection matters; past-date block removed.
    return


class PlanItemSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    chapter_name = serializers.CharField(source="chapter.name", read_only=True)
    topic_name = serializers.CharField(source="topic.name", read_only=True)
    reset_generated_title = serializers.BooleanField(write_only=True, required=False, default=False)
    generated_title = serializers.SerializerMethodField()
    counselor_editable = serializers.SerializerMethodField()
    edit_lock_reason = serializers.SerializerMethodField()

    class Meta:
        model = PlanItem
        fields = ("id", "taxonomy", "title_is_custom", "generated_title", "reset_generated_title", "plan_day", "kind", "ordering", "title", "planned_duration_minutes", "start_time", "end_time", "note", "subject", "subject_name", "chapter", "chapter_name", "topic", "topic_name", "test_count", "fixed_source_type", "fixed_source_id", "counselor_editable", "edit_lock_reason")

        read_only_fields = ("fixed_source_type", "fixed_source_id", "title_is_custom")

    def get_generated_title(self, obj):
        from .titles import build_title
        return build_title(self.selection(obj))

    @staticmethod
    def selection(obj):
        if obj.taxonomy:
            return obj.taxonomy
        topic, chapter, subject = obj.topic, obj.chapter, obj.subject
        chapter = chapter or (topic.chapter if topic else None)
        subject = subject or (chapter.subject if chapter else None)
        return {'labels': {k: v.name for k, v in
                (('course', subject), ('chapter', chapter), ('topic', topic)) if v}}

    def get_counselor_editable(self, obj):
        # Date no longer determines editability; only student data locks the item.
        return not item_has_student_data(obj)

    def get_edit_lock_reason(self, obj):
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
        from copy import copy
        from apps.academics.provider_catalog import resolve_selection
        from apps.exams.exceptions import Quiz24Error
        from .titles import build_title
        reset = attrs.pop('reset_generated_title', False)
        if reset and 'title' in attrs:
            raise serializers.ValidationError({'title': 'Reset and custom title cannot be supplied together.'})
        if 'taxonomy' in attrs:
            try:
                attrs['taxonomy'] = resolve_selection(attrs['taxonomy'])
            except Quiz24Error as error:
                raise serializers.ValidationError({'taxonomy': 'فهرست آموزشی موقتاً در دسترس نیست.'}) from error
        candidate = copy(self.instance) if self.instance else PlanItem()
        for key, value in attrs.items():
            setattr(candidate, key, value)
        if candidate.taxonomy and any((candidate.subject_id, candidate.chapter_id, candidate.topic_id)):
            raise serializers.ValidationError({'taxonomy': 'Internal and Quiz24 selections cannot be mixed.'})
        selection_changed = self.instance is None or bool(
            {'taxonomy', 'subject', 'chapter', 'topic', 'kind', 'plan_day'} & set(attrs))
        if candidate.kind != PlanItem.Kind.EVENT and selection_changed:
            if (not isinstance(candidate.taxonomy, dict)
                    or any(k not in candidate.taxonomy for k in ('grade', 'course', 'chapter'))):
                raise serializers.ValidationError({'taxonomy': 'Grade, course and chapter from Quiz24 are required.'})
            from apps.academics.provider_catalog import authorize_provider
            authorize_provider(day.plan.student.user, candidate.taxonomy['grade'], candidate.taxonomy['course'])
        generated = build_title(self.selection(candidate))
        custom = bool(attrs.get('title', '').strip()) if 'title' in attrs else (self.instance.title_is_custom if self.instance else False)
        if reset:
            custom = False
        if not custom:
            if not generated:
                raise serializers.ValidationError({'title': 'A title or structured selection is required.'})
            attrs['title'] = generated
        attrs['title_is_custom'] = custom
        return validate_model(self, attrs)


class PlanItemExecutionSerializer(serializers.ModelSerializer):
    elapsed_seconds = serializers.SerializerMethodField()
    completion_method = serializers.SerializerMethodField()

    class Meta:
        model = PlanItemExecution
        fields = ("plan_item", "source", "status", "started_at", "current_session_started_at", "accumulated_seconds", "elapsed_seconds", "completed_at", "completion_method")

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
        target_date = attrs.get("date", self.instance.date if self.instance else None)
        if self.instance and target_date != self.instance.date and (
            self.instance.items.filter(execution__isnull=False).exists()
            or self.instance.items.filter(report_items__isnull=False).exists()
        ):
            raise serializers.ValidationError({"date": "روز دارای عملکرد ثبت‌شده دانش‌آموز است و تاریخ آن قابل تغییر نیست."})
        # Past-date restriction removed: all 7 PlanDays are always active.
        # Only execution/report protection above remains.
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
        fields = ("id", "student", "counselor", "title", "start_date", "end_date", "status", "published_at", "created_at", "updated_at", "exam_dates")

    def validate(self, attrs):
        if self.instance and self.instance.status == Plan.Status.PUBLISHED:
            changed = set(self.initial_data) - {"title", "exam_dates"}
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
        # Validate exam_dates range against effective start/end
        if "exam_dates" in attrs:
            ed = attrs["exam_dates"]
            if ed is None:
                attrs["exam_dates"] = []
            elif not isinstance(ed, list):
                raise serializers.ValidationError({"exam_dates": "Must be a list of dates."})
            elif any(not isinstance(x, str) for x in ed):
                raise serializers.ValidationError({"exam_dates": "Each date must be YYYY-MM-DD."})
            # also check within range
            s = str(start) if start else None
            e = str(end) if end else None
            if s and e:
                for d in ed:
                    if not (s <= d <= e):
                        raise serializers.ValidationError({"exam_dates": "Exam dates must be within plan range."})
                        break
        return validate_model(self, attrs)


class PlanDetailSerializer(PlanSerializer):
    days = PlanDayDetailSerializer(many=True, read_only=True)

    class Meta(PlanSerializer.Meta):
        fields = PlanSerializer.Meta.fields + ("days",)


class StudentFixedCommitmentSerializer(serializers.ModelSerializer):
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all(), required=False)

    class Meta:
        model = StudentFixedCommitment
        fields = ("id", "student", "kind", "title", "weekday", "start_time", "end_time", "active", "group_id", "valid_from", "valid_until")
        read_only_fields = ("group_id",)

    def validate(self, attrs):
        user = self.context["request"].user
        student = attrs.get("student", self.instance.student if self.instance else None)
        # Resolve student when only provided via initial_data for create
        if not student and "student" in self.initial_data:
            try:
                student = StudentProfile.objects.get(pk=self.initial_data["student"])
                attrs["student"] = student
            except Exception:
                pass
        if user.role == User.Role.COUNSELOR:
            # Counselor may create/edit/delete fixed commitments for own students
            if student and not manageable_student(student, user):
                raise serializers.ValidationError({"student": "This student is not assigned to you."})
            if not student and not self.instance:
                raise serializers.ValidationError({"student": "This field is required."})
            if "student" not in attrs and self.instance:
                # On update, ensure still manageable
                if not manageable_student(self.instance.student, user):
                    raise serializers.ValidationError("This commitment is not available to you.")
        elif user.role == User.Role.STUDENT:
            if self.instance:
                raise serializers.ValidationError("Students cannot modify fixed commitments.")
            # If student sent explicit student id, validate it matches own
            explicit = self.initial_data.get("student")
            if explicit is not None and str(explicit) != str(user.student_profile.pk):
                raise serializers.ValidationError({"student": "Student must be your own profile."})
            if "student" not in attrs:
                attrs["student"] = user.student_profile
            else:
                # Ensure attrs student is own
                if attrs["student"].pk != user.student_profile.pk:
                    raise serializers.ValidationError({"student": "Student must be your own profile."})
            return validate_model(self, attrs)
        # Admin: allow but still validate student presence
        if "student" not in attrs and not self.instance:
            raise serializers.ValidationError({"student": "This field is required."})
        return validate_model(self, attrs)


class StudentSchoolSlotSerializer(serializers.ModelSerializer):
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all(), required=False)

    class Meta:
        model = StudentSchoolSlot
        fields = ("id", "student", "weekday", "period", "subject_name", "start_time", "end_time")

    def validate(self, attrs):
        user = self.context["request"].user
        student = attrs.get("student", self.instance.student if self.instance else None)
        if not student and "student" in self.initial_data:
            try:
                student = StudentProfile.objects.get(pk=self.initial_data["student"])
                attrs["student"] = student
            except Exception:
                pass
        if user.role == User.Role.COUNSELOR:
            if student and not manageable_student(student, user):
                raise serializers.ValidationError({"student": "This student is not assigned to you."})
            if not student and not self.instance:
                raise serializers.ValidationError({"student": "This field is required."})
            if "student" not in attrs and self.instance and not manageable_student(self.instance.student, user):
                raise serializers.ValidationError("This school slot is not available to you.")
        elif user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot modify school timetable.")
        if "student" not in attrs and not self.instance:
            raise serializers.ValidationError({"student": "This field is required."})
        return validate_model(self, attrs)


class StudentFixedEventSerializer(serializers.ModelSerializer):
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all(), required=False)

    class Meta:
        model = StudentFixedEvent
        fields = ("id", "student", "title", "date", "start_time", "end_time", "active", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        user = self.context["request"].user
        student = attrs.get("student", self.instance.student if self.instance else None)
        if not student and "student" in self.initial_data:
            try:
                student = StudentProfile.objects.get(pk=self.initial_data["student"])
                attrs["student"] = student
            except Exception:
                pass
        if user.role == User.Role.COUNSELOR:
            if student and not manageable_student(student, user):
                raise serializers.ValidationError({"student": "This student is not assigned to you."})
            if not student and not self.instance:
                raise serializers.ValidationError({"student": "This field is required."})
            if "student" not in attrs and self.instance and not manageable_student(self.instance.student, user):
                raise serializers.ValidationError("This fixed event is not available to you.")
        elif user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot modify fixed events.")
        if "student" not in attrs and not self.instance:
            raise serializers.ValidationError({"student": "This field is required."})
        return validate_model(self, attrs)


# Batch helpers (used by viewset actions)
class BatchCommitmentSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    kind = serializers.ChoiceField(choices=[c[0] for c in StudentFixedCommitment.Kind.choices])
    weekdays = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=6), allow_empty=False)
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all())
    valid_from = serializers.DateField(required=False, allow_null=True, default=None)
    valid_until = serializers.DateField(required=False, allow_null=True, default=None)

    def validate(self, attrs):
        if attrs["end_time"] <= attrs["start_time"]:
            raise serializers.ValidationError({"end_time": "End time must be after start time."})
        if attrs.get("valid_from") and attrs.get("valid_until") and attrs["valid_until"] < attrs["valid_from"]:
            raise serializers.ValidationError({"valid_until": "End date must be on or after start date."})
        if len(attrs["weekdays"]) != len(set(attrs["weekdays"])):
            raise serializers.ValidationError({"weekdays": "Duplicate weekdays are not allowed."})
        if not attrs["title"].strip():
            raise serializers.ValidationError({"title": "Title is required."})
        user = self.context["request"].user
        if user.role == User.Role.COUNSELOR and not manageable_student(attrs["student"], user):
            raise serializers.ValidationError({"student": "This student is not assigned to you."})
        if user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot modify fixed commitments.")
        return attrs


class SchoolHoursSerializer(serializers.Serializer):
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all())
    weekdays = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=6), allow_empty=False)
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    title = serializers.CharField(max_length=200, required=False, default="مدرسه")
    valid_from = serializers.DateField(required=False, allow_null=True, default=None)
    valid_until = serializers.DateField(required=False, allow_null=True, default=None)

    def validate(self, attrs):
        if attrs["end_time"] <= attrs["start_time"]:
            raise serializers.ValidationError({"end_time": "End time must be after start time."})
        if attrs.get("valid_from") and attrs.get("valid_until") and attrs["valid_until"] < attrs["valid_from"]:
            raise serializers.ValidationError({"valid_until": "End date must be on or after start date."})
        if len(attrs["weekdays"]) != len(set(attrs["weekdays"])):
            raise serializers.ValidationError({"weekdays": "Duplicate weekdays are not allowed."})
        user = self.context["request"].user
        if user.role == User.Role.COUNSELOR and not manageable_student(attrs["student"], user):
            raise serializers.ValidationError({"student": "This student is not assigned to you."})
        if user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot modify school timetable.")
        return attrs


class BulkSchoolSlotsSerializer(serializers.Serializer):
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all())
    slots = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False,
    )

    def validate_slots(self, value):
        seen = set()
        from django.utils.dateparse import parse_time
        for entry in value:
            try:
                wd = int(entry.get("weekday"))
                period = int(entry.get("period"))
                name = str(entry.get("subject_name", "")).strip()
            except Exception:
                raise serializers.ValidationError("Each slot needs weekday, period, subject_name.")
            if wd < 0 or wd > 6:
                raise serializers.ValidationError("Weekday must be 0-6.")
            if period < 1 or period > 12:
                raise serializers.ValidationError("Period must be 1-12.")
            if not name:
                raise serializers.ValidationError("Lesson name is required.")
            st = entry.get("start_time")
            en = entry.get("end_time")
            if (st is None) != (en is None):
                raise serializers.ValidationError("Start and end times must be supplied together for each slot.")
            if st and en:
                ps = parse_time(str(st))
                pe = parse_time(str(en))
                if not ps or not pe:
                    raise serializers.ValidationError("Invalid time in slot.")
                if pe <= ps:
                    raise serializers.ValidationError("Slot end time must be after start time.")
            key = (wd, period)
            if key in seen:
                raise serializers.ValidationError(f"Duplicate slot for weekday {wd} period {period}.")
            seen.add(key)
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.COUNSELOR and not manageable_student(attrs["student"], user):
            raise serializers.ValidationError({"student": "This student is not assigned to you."})
        if user.role == User.Role.STUDENT:
            raise serializers.ValidationError("Students cannot modify school timetable.")
        return attrs
