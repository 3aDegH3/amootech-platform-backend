from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("exams", "0003_student_practice")]
    operations = [migrations.CreateModel(
        name="ExamCreation",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("key", models.CharField(max_length=64, unique=True)),
            ("payload_digest", models.CharField(max_length=64)),
            ("state", models.CharField(default="PENDING", max_length=16)),
            ("response", models.JSONField(null=True)),
            ("error_status", models.PositiveIntegerField(null=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("exam", models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="creation", to="exams.exam")),
        ],
    )]
