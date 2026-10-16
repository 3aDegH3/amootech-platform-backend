from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("planning", "0008_plan_exam_dates")]
    operations = [
        migrations.AddField(model_name="planitem", name="fixed_source_type", field=models.CharField(blank=True, choices=[("school_slot", "School slot"), ("commitment", "Commitment"), ("event", "Event")], max_length=16)),
        migrations.AddField(model_name="planitem", name="fixed_source_id", field=models.PositiveBigIntegerField(blank=True, null=True)),
    ]
