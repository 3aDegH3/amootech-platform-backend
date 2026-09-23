from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.planning.models import PlanItem


def conflicting_activity(report, start, end, exclude_id=None):
    """Return the first published plan or student activity overlapping this interval."""
    if start is None or end is None:
        return None
    planned = PlanItem.objects.filter(
        plan_day__plan__student=report.student,
        plan_day__plan__status="PUBLISHED",
        plan_day__date=report.date,
        start_time__lt=end, end_time__gt=start,
    ).select_related("subject").order_by("start_time", "pk").first()
    if planned:
        return planned.title or (planned.subject.name if planned.subject_id else planned.get_kind_display())
    extras = DailyReportItem.objects.filter(
        report=report, plan_item__isnull=True,
        start_time__lt=end, end_time__gt=start,
    )
    if exclude_id:
        extras = extras.exclude(pk=exclude_id)
    extra = extras.select_related("subject").order_by("start_time", "pk").first()
    if extra:
        return extra.title or (extra.subject.name if extra.subject_id else extra.get_kind_display())
    return None


class DailyReport(models.Model):
    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.CASCADE, related_name="daily_reports")
    date = models.DateField()
    wake_time = models.TimeField(null=True, blank=True)
    sleep_time = models.TimeField(null=True, blank=True)
    mobile_minutes = models.PositiveIntegerField(null=True, blank=True)
    self_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    note = models.TextField(blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-date", "-pk")
        constraints = [
            models.UniqueConstraint(fields=("student", "date"), name="daily_report_one_per_student_date"),
            models.CheckConstraint(condition=Q(self_rating__isnull=True) | Q(self_rating__gte=1, self_rating__lte=20), name="daily_report_rating_1_to_20"),
        ]

    def clean(self):
        if self.self_rating is not None and not 1 <= self.self_rating <= 20:
            raise ValidationError({"self_rating": "Rating must be between 1 and 20."})


class DailyReportItem(models.Model):
    class DurationSource(models.TextChoices):
        MANUAL = "MANUAL", "Manual correction"
        PLAN = "PLAN", "Same as plan"

    report = models.ForeignKey(DailyReport, on_delete=models.CASCADE, related_name="items")
    plan_item = models.ForeignKey(PlanItem, on_delete=models.PROTECT, null=True, blank=True, related_name="report_items")
    # Academic fields are used only for an activity outside the plan.
    kind = models.CharField(max_length=8, choices=PlanItem.Kind.choices, blank=True)
    title = models.CharField(max_length=200, blank=True)
    resource = models.CharField(max_length=200, blank=True)
    note = models.CharField(max_length=500, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    subject = models.ForeignKey("academics.Subject", on_delete=models.PROTECT, null=True, blank=True)
    chapter = models.ForeignKey("academics.Chapter", on_delete=models.PROTECT, null=True, blank=True)
    topic = models.ForeignKey("academics.Topic", on_delete=models.PROTECT, null=True, blank=True)
    actual_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    duration_source = models.CharField(max_length=6, choices=DurationSource.choices, blank=True)
    actual_test_count = models.PositiveIntegerField(null=True, blank=True)
    correct_count = models.PositiveIntegerField(null=True, blank=True)
    wrong_count = models.PositiveIntegerField(null=True, blank=True)
    unanswered_count = models.PositiveIntegerField(null=True, blank=True)
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("ordering", "pk")
        constraints = [
            models.UniqueConstraint(fields=("report", "plan_item"), condition=Q(plan_item__isnull=False), name="daily_report_one_row_per_plan_item"),
        ]

    @property
    def activity_kind(self):
        return self.plan_item.kind if self.plan_item_id else self.kind

    def clean(self):
        errors = {}
        if self.plan_item_id:
            if self.plan_item.plan_day.date != self.report.date or self.plan_item.plan_day.plan.student_id != self.report.student_id:
                errors["plan_item"] = "Plan item must belong to this student and report date."
            if self.plan_item.plan_day.plan.status != "PUBLISHED":
                errors["plan_item"] = "Only published plan items can be reported."
            if any((self.kind, self.title, self.subject_id, self.chapter_id, self.topic_id)):
                errors["plan_item"] = "Planned academic details are inherited from the plan item."
            if self.resource or self.start_time or self.end_time:
                errors["plan_item"] = "منبع و ساعت رویداد فقط برای فعالیت خارج از برنامه است."
        else:
            if not self.kind:
                errors["kind"] = "Activity type is required."
            if self.kind in (PlanItem.Kind.STUDY, PlanItem.Kind.TEST, PlanItem.Kind.REVIEW) and not self.subject_id:
                errors["subject"] = "Subject is required for this activity type."
            if self.kind in (PlanItem.Kind.EXAM, PlanItem.Kind.EVENT) and not self.title.strip():
                errors["title"] = "Title is required for this activity type."
            if self.chapter_id and (not self.subject_id or self.chapter.subject_id != self.subject_id):
                errors["chapter"] = "Chapter must belong to the selected subject."
            if self.topic_id and (not self.chapter_id or self.topic.chapter_id != self.chapter_id):
                errors["topic"] = "Topic must belong to the selected chapter."
            if self.kind != PlanItem.Kind.EVENT and self.actual_duration_minutes is None and not (self.start_time and self.end_time):
                errors["actual_duration_minutes"] = "مدت فعالیت یا ساعت شروع و پایان را وارد کنید."
            if (self.start_time is None) != (self.end_time is None):
                errors["end_time"] = "ساعت شروع و پایان را با هم وارد کنید."
            elif self.start_time and self.end_time and self.end_time <= self.start_time:
                errors["end_time"] = "ساعت پایان باید پس از شروع باشد."
            if self.kind == PlanItem.Kind.EVENT:
                if self.resource:
                    errors["resource"] = "منبع فقط برای فعالیت درسی است."
                if self.actual_duration_minutes is None and not (self.start_time and self.end_time):
                    errors["actual_duration_minutes"] = "مدت رویداد یا ساعت شروع و پایان را وارد کنید."
            if self.start_time and self.end_time and self.end_time > self.start_time and self.report_id:
                conflict = conflicting_activity(self.report, self.start_time, self.end_time, self.pk)
                if conflict:
                    errors["start_time"] = f"این بازه زمانی با «{conflict}» تداخل دارد."
        if self.duration_source and self.actual_duration_minutes is None:
            errors["actual_duration_minutes"] = "Duration is required when a source is selected."
        if self.actual_duration_minutes is not None and not self.duration_source:
            errors["duration_source"] = "Duration source is required."
        if self.duration_source == self.DurationSource.PLAN and not self.plan_item_id:
            errors["duration_source"] = "Only a planned item can use its planned duration."
        if self.duration_source == self.DurationSource.PLAN and self.plan_item_id and self.actual_duration_minutes != self.plan_item.planned_duration_minutes:
            errors["actual_duration_minutes"] = "Duration must match the plan when using the plan shortcut."
        if self.activity_kind != PlanItem.Kind.TEST and any(value is not None for value in (self.actual_test_count, self.correct_count, self.wrong_count, self.unanswered_count)):
            errors["actual_test_count"] = "Test counts are only used for test activities."
        details = (self.correct_count, self.wrong_count, self.unanswered_count)
        if any(value is not None for value in details):
            if self.actual_test_count is None:
                errors["actual_test_count"] = "Enter the actual test count before the breakdown."
            elif sum(value or 0 for value in details) > self.actual_test_count:
                errors["actual_test_count"] = "مجموع پاسخ‌های صحیح، غلط و نزده نمی‌تواند بیشتر از تعداد تست‌ها باشد."
            elif all(value is not None for value in details) and sum(details) != self.actual_test_count:
                errors["actual_test_count"] = "مجموع پاسخ‌های صحیح، غلط و نزده باید با تعداد تست‌ها برابر باشد."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
