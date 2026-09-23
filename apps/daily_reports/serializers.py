from rest_framework import serializers

from apps.academics.models import Subject, Chapter, Topic
from apps.planning.models import PlanItem
from .models import DailyReport, DailyReportItem


class OpenReportSerializer(serializers.Serializer):
    date = serializers.DateField()


class CloseDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    self_rating = serializers.IntegerField(min_value=1, max_value=20)
    note = serializers.CharField(max_length=500, allow_blank=False, trim_whitespace=True)


class DailyFieldsSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyReport
        fields = ("wake_time", "sleep_time", "mobile_minutes", "self_rating", "note")

    def validate_self_rating(self, value):
        if value is not None and not 1 <= value <= 20:
            raise serializers.ValidationError("امتیاز باید بین ۱ و ۲۰ باشد.")
        return value


class ReportItemInputSerializer(serializers.Serializer):
    completion_status = serializers.ChoiceField(choices=("COMPLETED", "PARTIAL"), required=False)
    note = serializers.CharField(max_length=500, allow_blank=True, required=False)
    actual_duration_minutes = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    duration_source = serializers.ChoiceField(choices=DailyReportItem.DurationSource.choices, allow_blank=True, required=False)
    actual_test_count = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    correct_count = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    wrong_count = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    unanswered_count = serializers.IntegerField(min_value=0, allow_null=True, required=False)


class UnplannedItemInputSerializer(ReportItemInputSerializer):
    completion_status = None
    kind = serializers.ChoiceField(choices=PlanItem.Kind.choices)
    title = serializers.CharField(max_length=200, allow_blank=True, required=False)
    resource = serializers.CharField(max_length=200, allow_blank=True, required=False)
    start_time = serializers.TimeField(allow_null=True, required=False)
    end_time = serializers.TimeField(allow_null=True, required=False)
    subject = serializers.PrimaryKeyRelatedField(queryset=Subject.objects.all(), allow_null=True, required=False)
    chapter = serializers.PrimaryKeyRelatedField(queryset=Chapter.objects.all(), allow_null=True, required=False)
    topic = serializers.PrimaryKeyRelatedField(queryset=Topic.objects.all(), allow_null=True, required=False)
