from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ('planning', '0007_slot_times_and_fixed_events'),
    ]
    operations = [
        migrations.AddField(
            model_name='plan',
            name='exam_dates',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
