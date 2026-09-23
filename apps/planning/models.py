from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


class Plan(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"

    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.CASCADE, related_name="plans")
    counselor = models.ForeignKey("accounts.CounselorProfile", on_delete=models.PROTECT, related_name="plans")
    title = models.CharField(max_length=200, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-start_date", "-pk")
        constraints = [models.CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="planning_plan_valid_range")]

    def clean(self):
        errors = {}
        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors["end_date"] = "End date must be on or after start date."
        if self.student_id and self.counselor_id and self.student.counselor_id != self.counselor_id:
            errors["counselor"] = "Counselor must be assigned to this student."
        if self.status == self.Status.DRAFT and self.published_at:
            errors["published_at"] = "Draft plans cannot have a publication date."
        if self.status == self.Status.PUBLISHED and not self.published_at:
            errors["published_at"] = "Published plans require a publication date."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PlanDay(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="days")
    date = models.DateField()

    class Meta:
        ordering = ("date", "pk")
        constraints = [models.UniqueConstraint(fields=("plan", "date"), name="planning_unique_day_per_plan")]

    def clean(self):
        if self.plan_id and self.date and not (self.plan.start_date <= self.date <= self.plan.end_date):
            raise ValidationError({"date": "Date must be within the plan range."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PlanItem(models.Model):
    class Kind(models.TextChoices):
        STUDY = "STUDY", "Study"
        TEST = "TEST", "Test"
        REVIEW = "REVIEW", "Review"
        EXAM = "EXAM", "Exam"
        EVENT = "EVENT", "Event"

    plan_day = models.ForeignKey(PlanDay, on_delete=models.CASCADE, related_name="items")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    ordering = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=200, blank=True)
    planned_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    subject = models.ForeignKey("academics.Subject", on_delete=models.PROTECT, null=True, blank=True)
    chapter = models.ForeignKey("academics.Chapter", on_delete=models.PROTECT, null=True, blank=True)
    topic = models.ForeignKey("academics.Topic", on_delete=models.PROTECT, null=True, blank=True)
    test_count = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ("ordering", "pk")
        constraints = [
            models.CheckConstraint(condition=Q(planned_duration_minutes__isnull=True) | Q(planned_duration_minutes__gt=0), name="planning_item_positive_duration"),
            models.CheckConstraint(condition=Q(test_count__isnull=True) | Q(test_count__gt=0), name="planning_item_positive_test_count"),
        ]

    def clean(self):
        errors = {}
        if self.kind != self.Kind.EVENT and not self.planned_duration_minutes:
            errors["planned_duration_minutes"] = "Duration is required for this item type."
        if self.kind in (self.Kind.STUDY, self.Kind.TEST, self.Kind.REVIEW) and not self.subject_id:
            errors["subject"] = "Subject is required for this item type."
        if self.kind in (self.Kind.EXAM, self.Kind.EVENT) and not self.title.strip():
            errors["title"] = "Title is required for this item type."
        if self.kind == self.Kind.TEST and not self.test_count:
            errors["test_count"] = "Test items require a positive test count."
        if self.kind != self.Kind.TEST and self.test_count is not None:
            errors["test_count"] = "Test count is only used for test items."
        if self.chapter_id and self.subject_id and self.chapter.subject_id != self.subject_id:
            errors["chapter"] = "Chapter must belong to the selected subject."
        if self.topic_id:
            if self.chapter_id and self.topic.chapter_id != self.chapter_id:
                errors["topic"] = "Topic must belong to the selected chapter."
            if self.subject_id and self.topic.chapter.subject_id != self.subject_id:
                errors["topic"] = "Topic must belong to the selected subject."
        if (self.start_time is None) != (self.end_time is None):
            errors["end_time"] = "Start and end times must be supplied together."
        elif self.start_time and self.end_time and self.end_time <= self.start_time:
            errors["end_time"] = "End time must be after start time."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PlanItemExecution(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        PAUSED = "PAUSED", "Paused"
        COMPLETED = "COMPLETED", "Completed"
        PARTIAL = "PARTIAL", "Partial"
        NOT_DONE = "NOT_DONE", "Not done"

    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.CASCADE, related_name="plan_executions")
    plan_item = models.OneToOneField(PlanItem, on_delete=models.CASCADE, related_name="execution")
    status = models.CharField(max_length=12, choices=Status.choices)
    started_at = models.DateTimeField(null=True, blank=True)
    current_session_started_at = models.DateTimeField(null=True, blank=True)
    accumulated_seconds = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("student",), condition=Q(status="IN_PROGRESS"),
                name="planning_one_active_timer_per_student",
            ),
        ]

    def elapsed_seconds(self, now):
        running = max(0, int((now - self.current_session_started_at).total_seconds())) if self.current_session_started_at else 0
        return self.accumulated_seconds + running


class StudentFixedCommitment(models.Model):
    class Kind(models.TextChoices):
        SCHOOL = "SCHOOL", "School"
        CLASS = "CLASS", "Class"
        SPORTS = "SPORTS", "Sports"
        COMMUTE = "COMMUTE", "Commute"
        OTHER = "OTHER", "Other"

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.CASCADE, related_name="fixed_commitments")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    title = models.CharField(max_length=200)
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("weekday", "start_time", "pk")

    def clean(self):
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({"end_time": "End time must be after start time."})
        if not self.title.strip():
            raise ValidationError({"title": "Title is required."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
