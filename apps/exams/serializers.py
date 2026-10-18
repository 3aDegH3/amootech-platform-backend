from django.core.validators import URLValidator
from rest_framework import serializers

from apps.accounts.models import CounselorProfile, StudentProfile, User
from .models import Exam, ExamAnswer, ExamAttempt


class CourseDetailSerializer(serializers.Serializer):
    grade_id = serializers.IntegerField(min_value=1)
    course_id = serializers.IntegerField(min_value=1)
    consider_used_question = serializers.BooleanField(default=False)


class ExamSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    result_available = serializers.BooleanField(read_only=True, default=False)

    class Meta:
        model = Exam
        fields = (
            "id", "student", "student_name", "counselor", "title", "provider", "result_available",
            "provider_exam_id", "provider_exam_code", "grade_external_id", "question_type",
            "duration_minutes", "has_negative_score", "status", "question_count", "manage_url",
            "question_file_url", "answer_file_url", "key_file_url", "created_at", "updated_at",
        )
        read_only_fields = fields

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
            "id", "exam", "student", "status", "score", "balance", "rank",
            "correct_count", "wrong_count", "no_answer_count", "started_at", "finished_at",
            "created_at", "updated_at", "answers",
        )
        read_only_fields = fields


class StudentExamSerializer(serializers.ModelSerializer):
    result_available = serializers.BooleanField(read_only=True, default=False)
    counselor_name = serializers.SerializerMethodField()

    def get_counselor_name(self, obj):
        return (obj.counselor.user.get_full_name() or obj.counselor.user.username) if obj.counselor_id else None

    class Meta:
        model = Exam
        fields = ("id", "title", "question_count", "question_type", "duration_minutes", "status", "created_at", "counselor_name", "result_available", "is_personal_practice")
        read_only_fields = fields


class CreateExamSerializer(serializers.Serializer):
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
