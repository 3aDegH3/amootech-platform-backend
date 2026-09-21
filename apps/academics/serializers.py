from rest_framework import serializers
from .models import Grade, Field, Subject, Chapter, Topic


class GradeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Grade
        fields = ("id", "name", "ordering", "is_active")


class FieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = Field
        fields = ("id", "grade", "name", "ordering", "is_active")


class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ("id", "field", "name", "ordering", "is_active")


class ChapterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chapter
        fields = ("id", "subject", "name", "ordering", "is_active")


class TopicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Topic
        fields = ("id", "chapter", "name", "ordering", "is_active")
