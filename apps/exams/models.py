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
    archived_at = models.DateTimeField(null=True, blank=True)
    trashed_at = models.DateTimeField(null=True, blank=True)
    ever_published = models.BooleanField(default=False)
    creation_mode = models.CharField(max_length=12, default="UNKNOWN")
    title = models.CharField(max_length=200)
    provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.QUIZ24)
    provider_exam_id = models.CharField(max_length=100, null=True, blank=True)
    provider_exam_code = models.CharField(max_length=100, null=True, blank=True)
    grade_external_id = models.CharField(max_length=100, null=True, blank=True)
    question_type = models.CharField(max_length=32, null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    scheduled_start_at = models.DateTimeField(null=True, blank=True)
    participation_deadline_at = models.DateTimeField(null=True, blank=True)
    result_release_at = models.DateTimeField(null=True, blank=True)
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


class ExamAssignment(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="assignments")
    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.CASCADE, related_name="exam_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ("pk",)
        constraints = [models.UniqueConstraint(fields=("exam", "student"), name="exams_unique_assignment_student")]


class ExamAttempt(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="attempts")
    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.CASCADE, related_name="exam_attempts")
    provider_username = models.CharField(max_length=150)
    participation_token = models.UUIDField(null=True, blank=True)
    participation_lease_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    score = models.FloatField(null=True, blank=True)
    balance = models.FloatField(null=True, blank=True)
    rank = models.PositiveIntegerField(null=True, blank=True)
    provider_rank_in_class = models.PositiveIntegerField(null=True, blank=True)
    result_verified_at = models.DateTimeField(null=True, blank=True)
    result_identity_digest = models.CharField(max_length=64, blank=True, default="")
    result_exam_code = models.CharField(max_length=100, blank=True, default="")
    score_source = models.CharField(max_length=16, blank=True, default="")
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


class ExamMonitoring(models.Model):
    exam = models.OneToOneField(Exam, on_delete=models.CASCADE, related_name="monitoring")
    last_attempt_at = models.DateTimeField(null=True)
    last_success_at = models.DateTimeField(null=True)
    error = models.CharField(max_length=32, blank=True)
    partial = models.BooleanField(default=True)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)


class ExamParticipation(models.Model):
    class Status(models.TextChoices):
        NOT_STARTED = "NOT_STARTED", "Not started"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"

    assignment = models.OneToOneField(ExamAssignment, on_delete=models.CASCADE, related_name="participation")
    status = models.CharField(max_length=16, choices=Status.choices)
    observed_at = models.DateTimeField()
    identity_student_id = models.PositiveBigIntegerField()
    identity_school_id = models.CharField(max_length=10)
    identity_verified_at = models.DateTimeField()


class ExamAnalyticsSync(models.Model):
    exam = models.OneToOneField(Exam, on_delete=models.CASCADE, related_name="analytics_sync")
    last_attempt_at = models.DateTimeField(null=True)
    last_success_at = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    cursor_assignment_id = models.PositiveBigIntegerField(default=0)
    last_batch_size = models.PositiveIntegerField(default=0)
    last_error_count = models.PositiveIntegerField(default=0)
    partial = models.BooleanField(default=True)
    error = models.CharField(max_length=32, blank=True)


class ExamProviderOperation(models.Model):
    exam = models.OneToOneField(Exam, on_delete=models.PROTECT, related_name="provider_operation")
    kind = models.CharField(max_length=12)
    state = models.CharField(max_length=12)
    token = models.UUIDField()
    actor = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    requested_at = models.DateTimeField(auto_now=True)
    response_url = models.URLField(max_length=2000, blank=True)


class ExamManagementEvent(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.PROTECT, related_name="management_events")
    actor = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=32)
    previous = models.JSONField(default=dict)
    current = models.JSONField(default=dict)
    outcome = models.CharField(max_length=16, default="SUCCESS")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [models.Index(fields=("exam", "-created_at"), name="exams_management_history")]
