from django.db import models


class Exam(models.Model):
    class Provider(models.TextChoices):
        QUIZ24 = "QUIZ24", "Quiz24"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        READY = "READY", "Ready"
        PUBLISHED = "PUBLISHED", "Published"
        CLOSED = "CLOSED", "Closed"

    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="exams",
    )
    counselor = models.ForeignKey(
        "accounts.CounselorProfile", on_delete=models.PROTECT, related_name="exams", null=True, blank=True,
    )
    is_personal_practice = models.BooleanField(default=False)
    title = models.CharField(max_length=200)
    provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.QUIZ24)
    provider_exam_id = models.CharField(max_length=100, null=True, blank=True)
    provider_exam_code = models.CharField(max_length=100, null=True, blank=True)
    grade_external_id = models.CharField(max_length=100, null=True, blank=True)
    question_type = models.CharField(max_length=32, null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    has_negative_score = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    question_count = models.PositiveIntegerField(null=True, blank=True)
    manage_url = models.URLField(max_length=2000, null=True, blank=True)
    question_file_url = models.URLField(max_length=2000, null=True, blank=True)
    answer_file_url = models.URLField(max_length=2000, null=True, blank=True)
    key_file_url = models.URLField(max_length=2000, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "provider_exam_id"), name="exams_unique_provider_exam",
            ),
        ]


class ExamAttempt(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="attempts")
    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.CASCADE, related_name="exam_attempts")
    provider_username = models.CharField(max_length=150)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    score = models.FloatField(null=True, blank=True)
    balance = models.FloatField(null=True, blank=True)
    rank = models.PositiveIntegerField(null=True, blank=True)
    correct_count = models.PositiveIntegerField(null=True, blank=True)
    wrong_count = models.PositiveIntegerField(null=True, blank=True)
    no_answer_count = models.PositiveIntegerField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    raw_result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("exam", "student"), name="exams_unique_attempt_student"),
        ]


class ExamAnswer(models.Model):
    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name="answers")
    order = models.PositiveIntegerField()
    answer = models.TextField(null=True, blank=True)
    correct_answer = models.TextField(null=True, blank=True)
    is_correct = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ("order", "pk")
        constraints = [
            models.UniqueConstraint(fields=("attempt", "order"), name="exams_unique_answer_order"),
        ]


class ExamCreation(models.Model):
    """Durable receipt: never repeat a provider side effect after an uncertain outcome."""

    key = models.CharField(max_length=64, unique=True)
    payload_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="PENDING")
    response = models.JSONField(null=True)
    error_status = models.PositiveIntegerField(null=True)
    exam = models.OneToOneField(Exam, null=True, on_delete=models.PROTECT, related_name="creation")
    created_at = models.DateTimeField(auto_now_add=True)
