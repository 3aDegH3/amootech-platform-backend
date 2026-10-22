from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('exams', '0010_examattempt_participation_lease_until_and_more')]
    operations = [migrations.AddField(model_name='examcreation', name='confirmed_scope',
                 field=models.JSONField(default=dict, blank=True, editable=False))]
