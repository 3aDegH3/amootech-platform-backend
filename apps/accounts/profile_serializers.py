from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from .mobile import normalize_mobile
from rest_framework import serializers
from .models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field


class ProfileUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "is_active", "date_joined", "last_login")


class ProfileSerializer(serializers.ModelSerializer):
    user = ProfileUserSerializer(read_only=True)
    username = serializers.CharField(write_only=True, required=False)
    password = serializers.CharField(write_only=True, required=False)
    email = serializers.EmailField(write_only=True, required=False, allow_blank=True)
    first_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    last_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    is_active = serializers.BooleanField(write_only=True, required=False)

    def validate(self, attrs):
        if self.instance is None:
            for key in ("username", "password"):
                if not attrs.get(key):
                    raise serializers.ValidationError({key: "This field is required."})
            if User.objects.filter(username=attrs["username"]).exists():
                raise serializers.ValidationError({"username": "This username is already in use."})
        elif "username" in attrs and User.objects.exclude(pk=self.instance.user_id).filter(username=attrs["username"]).exists():
            raise serializers.ValidationError({"username": "This username is already in use."})
        return attrs

    def save_user(self, attrs, role):
        user_fields = {key: attrs.pop(key) for key in list(attrs) if key in ("username", "password", "email", "first_name", "last_name", "is_active")}
        if self.instance is None:
            password = user_fields.pop("password")
            return User.objects.create_user(password=password, role=role, **user_fields)
        user = self.instance.user
        if "password" in user_fields:
            user.set_password(user_fields.pop("password"))
        for key, value in user_fields.items():
            setattr(user, key, value)
        user.save()
        return user


class CounselorSerializer(ProfileSerializer):
    students_count = serializers.IntegerField(read_only=True)
    active_students_count = serializers.IntegerField(read_only=True)
    plans_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CounselorProfile
        fields = ("id", "user", "username", "password", "email", "first_name", "last_name", "is_active", "students_count", "active_students_count", "plans_count")

    @transaction.atomic
    def create(self, validated_data):
        return CounselorProfile.objects.create(user=self.save_user(validated_data, User.Role.COUNSELOR))

    @transaction.atomic
    def update(self, instance, validated_data):
        self.save_user(validated_data, User.Role.COUNSELOR)
        return instance


MOBILE_FIELDS = ("mobile", "mobile_verified_at", "quiz24_student_id", "quiz24_sync_status", "quiz24_sync_error", "quiz24_sync_at")
MOBILE_READONLY = MOBILE_FIELDS[1:]


class StudentSerializer(ProfileSerializer):
    mobile = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_mobile(self, value):
        if not value:
            return None
        try:
            value = normalize_mobile(value)
        except DjangoValidationError:
            raise serializers.ValidationError("شماره موبایل معتبر نیست.")
        if StudentProfile.objects.filter(mobile=value).exclude(pk=self.instance.pk if self.instance else None).exists():
            raise serializers.ValidationError("این شماره قابل استفاده نیست.")
        return value

    counselor_user = ProfileUserSerializer(source="counselor.user", read_only=True)
    grade_name = serializers.CharField(source="grade.name", read_only=True)
    field_name = serializers.CharField(source="field.name", read_only=True)
    current_plan = serializers.SerializerMethodField()
    telegram_status = serializers.SerializerMethodField()
    reports_today = serializers.SerializerMethodField()

    class Meta:
        model = StudentProfile
        fields = ("id", "user", "username", "password", "email", "first_name", "last_name", "is_active", "counselor", "counselor_user", "grade", "grade_name", "field", "field_name", "school_name", "current_plan", "telegram_status", "reports_today") + MOBILE_FIELDS
        read_only_fields = MOBILE_READONLY

    def get_current_plan(self, obj):
        # Annotated or fallback: look for current published plan
        if hasattr(obj, "_current_plan_cache"):
            cp = obj._current_plan_cache
            if cp:
                return {"id": cp.id, "title": cp.title, "status": cp.status, "start_date": cp.start_date.isoformat(), "end_date": cp.end_date.isoformat()}
            return None
        # Fallback without annotation (single object)
        from apps.planning.models import Plan
        from django.utils import timezone
        today = timezone.localdate()
        cp = Plan.objects.filter(student=obj, status=Plan.Status.PUBLISHED, start_date__lte=today, end_date__gte=today).order_by("-start_date").first()
        if cp:
            return {"id": cp.id, "title": cp.title, "status": cp.status, "start_date": cp.start_date.isoformat(), "end_date": cp.end_date.isoformat()}
        latest = Plan.objects.filter(student=obj).order_by("-created_at").first()
        if latest:
            return {"id": latest.id, "title": latest.title, "status": latest.status, "start_date": latest.start_date.isoformat(), "end_date": latest.end_date.isoformat()}
        return None

    def get_telegram_status(self, obj):
        if hasattr(obj, "_telegram_cache"):
            return obj._telegram_cache
        return None

    def get_reports_today(self, obj):
        if hasattr(obj, "_reports_today_cache"):
            return obj._reports_today_cache
        return None

    def validate(self, attrs):
        if set(self.initial_data) & set(MOBILE_READONLY):
            raise serializers.ValidationError("وضعیت تأیید و اتصال را نمی‌توان به‌صورت دستی تغییر داد.")
        attrs = super().validate(attrs)
        grade = attrs.get("grade", self.instance.grade if self.instance else None)
        field = attrs.get("field", self.instance.field if self.instance else None)
        if field and (not grade or field.grade_id != grade.pk):
            raise serializers.ValidationError({"field": "Field must belong to the selected grade."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        try:
            return StudentProfile.objects.create(user=self.save_user(validated_data, User.Role.STUDENT), **validated_data)
        except (IntegrityError, DjangoValidationError):
            raise serializers.ValidationError({"mobile": "اطلاعات حساب یا شماره موبایل قابل ثبت نیست."}) from None

    @transaction.atomic
    def update(self, instance, validated_data):
        instance = StudentProfile.objects.select_for_update().get(pk=instance.pk)
        self.save_user(validated_data, User.Role.STUDENT)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        try:
            instance.save()
        except (IntegrityError, DjangoValidationError):
            raise serializers.ValidationError({"mobile": "اطلاعات حساب یا شماره موبایل قابل ثبت نیست."}) from None
        return instance


class StudentRegistrationSerializer(StudentSerializer):
    username = serializers.CharField(write_only=True, required=True)
    password = serializers.CharField(write_only=True, required=True)
    first_name = serializers.CharField(write_only=True, required=True, allow_blank=False)
    last_name = serializers.CharField(write_only=True, required=True, allow_blank=False)
    grade = serializers.PrimaryKeyRelatedField(queryset=Grade.objects.filter(is_active=True), required=True)
    field = serializers.PrimaryKeyRelatedField(queryset=Field.objects.filter(is_active=True, grade__is_active=True), required=True)

    class Meta(StudentSerializer.Meta):
        fields = ("id", "user", "username", "password", "email", "first_name", "last_name", "grade", "field", "school_name") + MOBILE_FIELDS
        read_only_fields = MOBILE_READONLY

    def validate(self, attrs):
        forbidden = {"role", "counselor", "is_active", "is_staff", "is_superuser"} & set(self.initial_data)
        if forbidden:
            raise serializers.ValidationError({key: "This field cannot be set during registration." for key in forbidden})
        return super().validate(attrs)


class StudentSelfSerializer(StudentSerializer):
    class Meta(StudentSerializer.Meta):
        fields = ("id", "user", "email", "first_name", "last_name", "grade", "grade_name", "field", "field_name", "school_name", "counselor_user") + MOBILE_FIELDS
        read_only_fields = MOBILE_READONLY

    def validate(self, attrs):
        forbidden = {"role", "counselor", "is_active", "is_staff", "is_superuser", "username", "password"} & set(self.initial_data)
        if forbidden:
            raise serializers.ValidationError({key: "This field cannot be changed here." for key in forbidden})
        return super().validate(attrs)
