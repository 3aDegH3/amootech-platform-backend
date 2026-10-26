from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class Grade(models.Model):
    number = models.PositiveSmallIntegerField(null=True, blank=True, unique=True, validators=[MinValueValidator(1), MaxValueValidator(12)])
    name = models.CharField(max_length=100, unique=True)
    ordering = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("ordering", "name", "id")

    def __str__(self):
        return self.name


class Field(models.Model):
    grade = models.ForeignKey(Grade, on_delete=models.PROTECT, related_name="fields")
    name = models.CharField(max_length=100)
    ordering = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("ordering", "name", "id")
        constraints = [models.UniqueConstraint(fields=("grade", "name"), name="unique_field_per_grade")]

    def __str__(self):
        return self.name


class Subject(models.Model):
    field = models.ForeignKey(Field, on_delete=models.PROTECT, related_name="subjects")
    name = models.CharField(max_length=100)
    ordering = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("ordering", "name", "id")
        constraints = [models.UniqueConstraint(fields=("field", "name"), name="unique_subject_per_field")]

    def __str__(self):
        return self.name


class Chapter(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="chapters")
    name = models.CharField(max_length=100)
    ordering = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("ordering", "name", "id")
        constraints = [models.UniqueConstraint(fields=("subject", "name"), name="unique_chapter_per_subject")]

    def __str__(self):
        return self.name


class Topic(models.Model):
    chapter = models.ForeignKey(Chapter, on_delete=models.PROTECT, related_name="topics")
    name = models.CharField(max_length=100)
    ordering = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("ordering", "name", "id")
        constraints = [models.UniqueConstraint(fields=("chapter", "name"), name="unique_topic_per_chapter")]

    def __str__(self):
        return self.name    


class ProviderCatalogRevision(models.Model):
    """Immutable publication, including provider envelopes for existing API contracts."""
    provider = models.CharField(max_length=20, default='quiz24', editable=False)
    started_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    counts = models.JSONField(default=dict)
    payload = models.JSONField(default=dict)


class ProviderCatalogState(models.Model):
    requested = models.BooleanField(default=False)
    revision = models.ForeignKey(ProviderCatalogRevision, null=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, default='NEVER')
    last_started = models.DateTimeField(null=True)
    last_success = models.DateTimeField(null=True)
    error = models.TextField(blank=True)


class ProviderCatalogNode(models.Model):
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    canonical_grade_number = models.PositiveSmallIntegerField(null=True, blank=True)
    academic_field = models.CharField(max_length=100, blank=True)
    provider = models.CharField(max_length=20, default='quiz24', editable=False)
    kind = models.CharField(max_length=12)
    provider_id = models.PositiveIntegerField()
    grade_id_external = models.PositiveIntegerField(default=0)
    course_id_external = models.PositiveIntegerField(default=0)
    parent_key = models.CharField(max_length=80, blank=True)
    title = models.CharField(max_length=500)
    level = models.PositiveSmallIntegerField()
    ordering = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict)
    active = models.BooleanField(default=True)
    verified_at = models.DateTimeField()
    revision = models.ForeignKey(ProviderCatalogRevision, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('provider', 'kind', 'grade_id_external', 'course_id_external', 'provider_id'), name='unique_provider_catalog_path')]
        indexes = [models.Index(fields=('kind', 'parent_key', 'active')), models.Index(fields=('grade_id_external', 'course_id_external', 'active'))]


class ProviderCatalogSyncRun(models.Model):
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    status = models.CharField(max_length=12)
    revision = models.ForeignKey(ProviderCatalogRevision, null=True, on_delete=models.PROTECT)
    counts = models.JSONField(default=dict)
    error = models.TextField(blank=True)
