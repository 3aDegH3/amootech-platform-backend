from django.db import transaction
from rest_framework import serializers
from .models import User, StudentProfile, CounselorProfile
from apps.academics.models import Grade, Field


class ProfileUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "is_active")


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
    class Meta:
        model = CounselorProfile
        fields = ("id", "user", "username", "password", "email", "first_name", "last_name", "is_active")

    @transaction.atomic
    def create(self, validated_data):
        return CounselorProfile.objects.create(user=self.save_user(validated_data, User.Role.COUNSELOR))

    @transaction.atomic
    def update(self, instance, validated_data):
        self.save_user(validated_data, User.Role.COUNSELOR)
        return instance


class StudentSerializer(ProfileSerializer):
    counselor_user = ProfileUserSerializer(source="counselor.user", read_only=True)
    grade_name = serializers.CharField(source="grade.name", read_only=True)
    field_name = serializers.CharField(source="field.name", read_only=True)

    class Meta:
        model = StudentProfile
        fields = ("id", "user", "username", "password", "email", "first_name", "last_name", "is_active", "counselor", "counselor_user", "grade", "grade_name", "field", "field_name", "school_name")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        grade = attrs.get("grade", self.instance.grade if self.instance else None)
        field = attrs.get("field", self.instance.field if self.instance else None)
        if field and (not grade or field.grade_id != grade.pk):
            raise serializers.ValidationError({"field": "Field must belong to the selected grade."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        return StudentProfile.objects.create(user=self.save_user(validated_data, User.Role.STUDENT), **validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        self.save_user(validated_data, User.Role.STUDENT)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        return instance


class StudentRegistrationSerializer(StudentSerializer):
    username = serializers.CharField(write_only=True, required=True)
    password = serializers.CharField(write_only=True, required=True)
    first_name = serializers.CharField(write_only=True, required=True, allow_blank=False)
    last_name = serializers.CharField(write_only=True, required=True, allow_blank=False)
    grade = serializers.PrimaryKeyRelatedField(queryset=Grade.objects.filter(is_active=True), required=True)
    field = serializers.PrimaryKeyRelatedField(queryset=Field.objects.filter(is_active=True, grade__is_active=True), required=True)

    class Meta(StudentSerializer.Meta):
        fields = ("id", "user", "username", "password", "email", "first_name", "last_name", "grade", "field", "school_name")

    def validate(self, attrs):
        forbidden = {"role", "counselor", "is_active", "is_staff", "is_superuser"} & set(self.initial_data)
        if forbidden:
            raise serializers.ValidationError({key: "This field cannot be set during registration." for key in forbidden})
        return super().validate(attrs)


class StudentSelfSerializer(StudentSerializer):
    class Meta(StudentSerializer.Meta):
        fields = ("id", "user", "email", "first_name", "last_name", "grade", "grade_name", "field", "field_name", "school_name", "counselor_user")

    def validate(self, attrs):
        forbidden = {"role", "counselor", "is_active", "is_staff", "is_superuser", "username", "password"} & set(self.initial_data)
        if forbidden:
            raise serializers.ValidationError({key: "This field cannot be changed here." for key in forbidden})
        return super().validate(attrs)
