import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0002_counselorprofile_studentprofile"),
    ]

    operations = [
        migrations.CreateModel(
            name="Exam",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=200)),
                ("provider", models.CharField(choices=[("QUIZ24", "Quiz24")], default="QUIZ24", max_length=16)),
                ("provider_exam_id", models.CharField(blank=True, max_length=100, null=True)),
                ("provider_exam_code", models.CharField(blank=True, max_length=100, null=True)),
                ("grade_external_id", models.CharField(blank=True, max_length=100, null=True)),
                ("question_type", models.CharField(blank=True, max_length=32, null=True)),
                ("duration_minutes", models.PositiveIntegerField(blank=True, null=True)),
                ("has_negative_score", models.BooleanField(default=False)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("READY", "Ready"), ("PUBLISHED", "Published"), ("CLOSED", "Closed")], default="DRAFT", max_length=12)),
                ("question_count", models.PositiveIntegerField(blank=True, null=True)),
                ("manage_url", models.URLField(blank=True, max_length=2000, null=True)),
                ("question_file_url", models.URLField(blank=True, max_length=2000, null=True)),
                ("answer_file_url", models.URLField(blank=True, max_length=2000, null=True)),
                ("key_file_url", models.URLField(blank=True, max_length=2000, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("counselor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="exams", to="accounts.counselorprofile")),
                ("student", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="exams", to="accounts.studentprofile")),
            ],
            options={
                "ordering": ("-created_at", "-pk"),
                "constraints": [models.UniqueConstraint(fields=("provider", "provider_exam_id"), name="exams_unique_provider_exam")],
            },
        ),
    ]
