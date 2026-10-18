import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_counselorprofile_studentprofile"),
        ("exams", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExamAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider_username", models.CharField(max_length=150)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("COMPLETED", "Completed")], default="PENDING", max_length=12)),
                ("score", models.FloatField(blank=True, null=True)),
                ("balance", models.FloatField(blank=True, null=True)),
                ("rank", models.PositiveIntegerField(blank=True, null=True)),
                ("correct_count", models.PositiveIntegerField(blank=True, null=True)),
                ("wrong_count", models.PositiveIntegerField(blank=True, null=True)),
                ("no_answer_count", models.PositiveIntegerField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("raw_result", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("exam", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attempts", to="exams.exam")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="exam_attempts", to="accounts.studentprofile")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("exam", "student"), name="exams_unique_attempt_student")]},
        ),
        migrations.CreateModel(
            name="ExamAnswer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order", models.PositiveIntegerField()),
                ("answer", models.TextField(blank=True, null=True)),
                ("correct_answer", models.TextField(blank=True, null=True)),
                ("is_correct", models.BooleanField(blank=True, null=True)),
                ("attempt", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="answers", to="exams.examattempt")),
            ],
            options={
                "ordering": ("order", "pk"),
                "constraints": [models.UniqueConstraint(fields=("attempt", "order"), name="exams_unique_answer_order")],
            },
        ),
    ]
