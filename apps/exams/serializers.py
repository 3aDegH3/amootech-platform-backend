import re
from collections.abc import Mapping
from datetime import timedelta

from django.core.validators import URLValidator
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import CounselorProfile, StudentProfile, User
from .models import Exam, ExamAnswer, ExamAttempt
from .scheduling import SCHEDULE_FIELDS, availability, results_released


class CourseDetailSerializer(serializers.Serializer):
    grade_id = serializers.IntegerField(min_value=1)
    course_id = serializers.IntegerField(min_value=1)
    consider_used_question = serializers.BooleanField(default=False)


class SchedulePresentationMixin(serializers.Serializer):
    availability = serializers.SerializerMethodField()

    def get_availability(self, obj):
        return availability(obj)


class ExamSerializer(SchedulePresentationMixin, serializers.ModelSerializer):
    management_blocked = serializers.SerializerMethodField()

    def get_management_blocked(self, obj):
        from .services.management import blocked
        return obj.management_blocked if hasattr(obj, "management_blocked") else blocked(obj)

    schedule_locked = serializers.SerializerMethodField()

    def get_schedule_locked(self, obj):
        return obj.schedule_locked if hasattr(obj, "schedule_locked") else obj.attempts.exists()

    assignment_count = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    result_available = serializers.BooleanField(read_only=True, default=False)

    class Meta:
        model = Exam
        fields = (
            "id", "management_blocked", "archived_at", "trashed_at", "creation_mode", *SCHEDULE_FIELDS, "availability", "schedule_locked", "assignment_count", "student", "student_name", "counselor", "title", "provider", "result_available",
            "provider_exam_id", "provider_exam_code", "grade_external_id", "question_type",
            "duration_minutes", "has_negative_score", "status", "question_count", "manage_url",
            "question_file_url", "answer_file_url", "key_file_url", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_assignment_count(self, obj):
        if hasattr(obj, "assignment_count"):
            return obj.assignment_count
        rows = obj.assignments.all()
        request = self.context.get("request")
        if request and request.user.role == User.Role.COUNSELOR:
            rows = rows.filter(student__counselor=obj.counselor)
        return rows.count()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if instance.archived_at or instance.trashed_at or self.get_schedule_locked(instance) or getattr(instance, "participation_known", False) or getattr(instance, "management_blocked", False):
            data["manage_url"] = None
        if request and request.user.role == User.Role.COUNSELOR and instance.student_id and instance.student.counselor_id != instance.counselor_id:
            data.update(student=None, student_name=None, result_available=False)
        return data

    def get_student_name(self, obj):
        if not obj.student_id:
            return None
        return obj.student.user.get_full_name() or obj.student.user.username


class ExamAnswerSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamAnswer
        fields = ("order", "answer", "correct_answer", "is_correct")
        read_only_fields = fields


class ExamAttemptSerializer(serializers.ModelSerializer):
    answers = ExamAnswerSerializer(many=True, read_only=True)

    class Meta:
        model = ExamAttempt
        fields = (
            "id", "exam", "student", "status", "score", "balance", "rank", "provider_rank_in_class",
            "correct_count", "wrong_count", "no_answer_count", "started_at", "finished_at",
            "created_at", "updated_at", "answers",
        )
        read_only_fields = fields


class StudentExamSerializer(SchedulePresentationMixin, serializers.ModelSerializer):
    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not results_released(instance):
            data["result_available"] = False
        return data

    result_available = serializers.BooleanField(read_only=True, default=False)
    counselor_name = serializers.SerializerMethodField()

    def get_counselor_name(self, obj):
        return (obj.counselor.user.get_full_name() or obj.counselor.user.username) if obj.counselor_id else None

    class Meta:
        model = Exam
        fields = ("id", "archived_at", "trashed_at", *SCHEDULE_FIELDS, "availability", "title", "question_count", "question_type", "duration_minutes", "status", "created_at", "counselor_name", "result_available", "is_personal_practice")
        read_only_fields = fields


class CreateExamSerializer(serializers.Serializer):
    creation_key = serializers.UUIDField(required=False, write_only=True)
    student = serializers.PrimaryKeyRelatedField(queryset=StudentProfile.objects.all(), allow_null=True)
    title = serializers.CharField(max_length=200)
    grade_external_id = serializers.IntegerField(min_value=1, max_value=2147483647)
    question_type = serializers.ChoiceField(choices=(1, 2))
    duration_minutes = serializers.IntegerField(min_value=1, max_value=2147483647)
    has_negative_score = serializers.BooleanField(default=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = self.context["request"].user
        if user.role == User.Role.COUNSELOR:
            self.fields["student"].queryset = StudentProfile.objects.filter(counselor__user=user)

    def validate(self, attrs):
        user = self.context["request"].user
        if user.role == User.Role.STUDENT:
            student = StudentProfile.objects.select_related("counselor").filter(user=user).first()
            if student is None:
                raise serializers.ValidationError("پروفایل دانش‌آموز یافت نشد.")
            attrs["student"] = student
            attrs["counselor"] = student.counselor
            return attrs
        if user.role == User.Role.COUNSELOR:
            counselor = CounselorProfile.objects.filter(user=user).first()
        else:
            student = attrs.get("student")
            counselor = student.counselor if student else CounselorProfile.objects.filter(user=user).first()
        if not counselor:
            raise serializers.ValidationError("برای ساخت آزمون، مشاور مشخص یا دانش‌آموز دارای مشاور لازم است.")
        attrs["counselor"] = counselor
        return attrs


class AutomaticFiltersMixin:
    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get("object_ids") and (not attrs.get("subject_ids") or not attrs.get("chapter_ids")):
            raise serializers.ValidationError({"object_ids": "انتخاب زیرمبحث به فصل و مبحث نیاز دارد."})
        if attrs.get("subject_ids") and not attrs.get("chapter_ids"):
            raise serializers.ValidationError({"subject_ids": "انتخاب مبحث به فصل نیاز دارد."})
        if attrs.get("from_year") is not None and attrs.get("to_year") is not None and attrs["from_year"] > attrs["to_year"]:
            raise serializers.ValidationError({"to_year": "سال پایان باید برابر یا بعد از سال شروع باشد."})
        return attrs


class QuestionCountSerializer(AutomaticFiltersMixin, serializers.Serializer):
    grade_id = serializers.IntegerField(min_value=1, max_value=2147483647)
    course_id = serializers.IntegerField(min_value=1, max_value=2147483647)
    chapter_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=200)
    subject_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=200)
    object_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=200)
    question_type = serializers.ChoiceField(choices=(1, 2))
    from_year = serializers.IntegerField(min_value=1, max_value=9999, required=False)
    to_year = serializers.IntegerField(min_value=1, max_value=9999, required=False)
    consider_used_question = serializers.BooleanField(default=False)


class AutomaticExamSerializer(AutomaticFiltersMixin, CreateExamSerializer):
    creation_key = serializers.UUIDField(required=False, write_only=True)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.context["request"].user.role == User.Role.STUDENT:
            self.fields["student"].required = False
            self.fields["student"].queryset = StudentProfile.objects.none()

    def to_internal_value(self, data):
        if self.context["request"].user.role == User.Role.STUDENT and any(key in data for key in ("student", "student_id", "counselor", "counselor_id")):
            raise serializers.ValidationError({"detail": "آزمون تمرینی فقط برای حساب خودتان ساخته می‌شود؛ انتخاب دانش‌آموز مجاز نیست."})
        return super().to_internal_value(data)

    course_id = serializers.IntegerField(min_value=1, max_value=2147483647)
    chapter_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=200)
    subject_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=200)
    object_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=200)
    easy_count = serializers.IntegerField(min_value=0, max_value=2147483647, default=0)
    medium_count = serializers.IntegerField(min_value=0, max_value=2147483647, default=0)
    hard_count = serializers.IntegerField(min_value=0, max_value=2147483647, default=0)
    very_hard_count = serializers.IntegerField(min_value=0, max_value=2147483647, default=0)
    from_year = serializers.IntegerField(min_value=1, max_value=9999, required=False)
    to_year = serializers.IntegerField(min_value=1, max_value=9999, required=False)
    consider_used_question = serializers.BooleanField(default=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        total = sum(attrs[field] for field in ("easy_count", "medium_count", "hard_count", "very_hard_count"))
        if not 1 <= total <= 2147483647:
            raise serializers.ValidationError("حداقل یک سوال با مجموع معتبر انتخاب کنید.")
        return attrs


class AutomaticCreatedExamSerializer(serializers.Serializer):
    examCode = serializers.CharField(max_length=100)

    @classmethod
    def from_provider(cls, response):
        # https://www.quiz24.ir/WebService/v1: result is the created exam CODE,
        # not the exam ID. The live API also serializes this code as an integer.
        if "status" in response and str(response["status"]).strip() != "100":
            return cls(data={"examCode": None})
        result = response.get("result")
        code = response.get("examCode")
        if code is None:
            code = result.get("examCode") if isinstance(result, dict) else result
        if type(code) not in (str, int) or isinstance(code, int) and code <= 0:
            code = None
        return cls(data={"examCode": code})


class CreatedQuiz24ExamSerializer(serializers.Serializer):
    result = serializers.URLField(max_length=2000, validators=[URLValidator(schemes=("http", "https"))])
    examCode = serializers.CharField(max_length=100)

    @classmethod
    def from_provider(cls, response):
        result = response
        for _ in range(5):
            if not isinstance(result, dict) or ("examCode" in result and isinstance(result.get("result"), str)):
                break
            nested = result.get("result", result.get("data"))
            if not isinstance(nested, dict):
                break
            result = nested
        return cls(data=result)


class SyncedQuiz24ExamSerializer(serializers.Serializer):
    provider_exam_id = serializers.CharField(max_length=100)
    provider_exam_code = serializers.CharField(max_length=100, required=False)
    question_count = serializers.IntegerField(min_value=0, max_value=2147483647)
    # Quiz24 may return a file name/path rather than an absolute download URL.
    question_file_url = serializers.CharField(max_length=2000, allow_null=True, allow_blank=True, required=False)
    answer_file_url = serializers.CharField(max_length=2000, allow_null=True, allow_blank=True, required=False)
    key_file_url = serializers.CharField(max_length=2000, allow_null=True, allow_blank=True, required=False)

    @classmethod
    def from_provider(cls, response):
        result = response
        for _ in range(5):
            if isinstance(result, list) and len(result) == 1:
                result = result[0]
            elif isinstance(result, dict):
                if "id" in result and "questionCount" in result:
                    break
                nested = result.get("result", result.get("data", result.get("exam")))
                if nested is None:
                    break
                result = nested
            else:
                break
        if not isinstance(result, dict):
            result = {}
        # Provider names are mapped only here; ownership is never accepted.
        aliases = {
            "provider_exam_id": ("id",),
            "provider_exam_code": ("examCode",),
            "question_count": ("questionCount",),
            "question_file_url": ("questionFileName",),
            "answer_file_url": ("answerFileName",),
            "key_file_url": ("keyFileName",),
        }
        data = {}
        for field, keys in aliases.items():
            for key in keys:
                if key in result:
                    if field == "question_file_url" and key == "questionFileName" and not result[key]:
                        continue
                    data[field] = result[key]
                    break
        return cls(data=data)


class AssignmentInputSerializer(serializers.Serializer):
    student_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=200)


class ExplicitTimezoneDateTimeField(serializers.DateTimeField):
    def to_internal_value(self, value):
        if isinstance(value, str) and not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value):
            raise serializers.ValidationError("تاریخ باید ISO 8601 و دارای منطقه زمانی صریح باشد.")
        return super().to_internal_value(value)


class ExamScheduleSerializer(serializers.Serializer):
    scheduled_start_at = ExplicitTimezoneDateTimeField(required=False, allow_null=True)
    participation_deadline_at = ExplicitTimezoneDateTimeField(required=False, allow_null=True)
    result_release_at = ExplicitTimezoneDateTimeField(required=False, allow_null=True)

    def to_internal_value(self, data):
        if isinstance(data, Mapping) and set(data) - set(SCHEDULE_FIELDS):
            raise serializers.ValidationError({"non_field_errors": ["فقط فیلدهای زمان‌بندی قابل ویرایش هستند."]})
        return super().to_internal_value(data)

    def validate(self, attrs):
        exam = self.instance
        values = {key: attrs.get(key, getattr(exam, key)) for key in SCHEDULE_FIELDS}
        now = timezone.now()
        errors = {}
        for key, value in attrs.items():
            if value is not None and value != getattr(exam, key) and value <= now:
                errors[key] = "زمان انتخاب‌شده باید در آینده باشد."
        start, deadline, release = (values[key] for key in SCHEDULE_FIELDS)
        if start and deadline and deadline <= start:
            errors["participation_deadline_at"] = "مهلت شرکت باید پس از زمان شروع باشد."
        # The deadline is the exclusive last entry time. Release must also cover
        # a student entering just before it and using the configured duration.
        latest_start = deadline or start
        if release and latest_start:
            try:
                intended_end = latest_start + timedelta(minutes=exam.duration_minutes or 0)
            except OverflowError:
                errors["result_release_at"] = "زمان پایان آزمون خارج از محدوده معتبر است."
            else:
                if release < intended_end:
                    errors["result_release_at"] = "انتشار نتیجه باید پس از آخرین زمان مجاز شروع و مدت آزمون باشد."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class ManagementInputSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("rename", "unpublish", "archive", "unarchive", "trash", "restore"))
    expected_updated_at = serializers.DateTimeField()
    title = serializers.CharField(max_length=200, required=False, trim_whitespace=True)

    def validate(self, attrs):
        if attrs["action"] == "rename" and not attrs.get("title"):
            raise serializers.ValidationError({"title": "عنوان لازم است."})
        return attrs


class BulkExamItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1, max_value=9223372036854775807)
    expected_updated_at = serializers.DateTimeField()


class BulkManagementSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("archive", "unarchive", "trash"))
    exams = BulkExamItemSerializer(many=True, allow_empty=False, max_length=100)

    def validate_exams(self, value):
        if len({item["id"] for item in value}) != len(value):
            raise serializers.ValidationError("انتخاب تکراری مجاز نیست.")
        return value


class ProviderManagementSerializer(serializers.Serializer):
    request_key = serializers.UUIDField()
    expected_updated_at = serializers.DateTimeField()
    confirm_remote_delete = serializers.BooleanField(default=False)
    provider_exam_id = serializers.CharField(max_length=100, required=False)
    confirmed_title = serializers.CharField(max_length=200, required=False)
