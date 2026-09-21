from django.db import models


class Grade(models.Model):
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
