from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("exams", "0002_examattempt_examanswer")]

    operations = [
        migrations.AddField(
            model_name="exam", name="is_personal_practice",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="exam", name="counselor",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="exams", to="accounts.counselorprofile"),
        ),
    ]
