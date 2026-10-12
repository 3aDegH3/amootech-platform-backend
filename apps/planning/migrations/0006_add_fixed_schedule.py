# Generated for fixed weekly schedule management
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_studentprofile_school_name'),
        ('planning', '0005_planitemexecution_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='studentfixedcommitment',
            name='group_id',
            field=models.UUIDField(blank=True, null=True, db_index=True),
        ),
        migrations.AddField(
            model_name='studentfixedcommitment',
            name='valid_from',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='studentfixedcommitment',
            name='valid_until',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='StudentSchoolSlot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('weekday', models.PositiveSmallIntegerField(choices=[(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')])),
                ('period', models.PositiveSmallIntegerField()),
                ('subject_name', models.CharField(max_length=120)),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='school_slots', to='accounts.studentprofile')),
            ],
            options={
                'ordering': ('weekday', 'period', 'pk'),
            },
        ),
        migrations.AddConstraint(
            model_name='studentschoolslot',
            constraint=models.UniqueConstraint(fields=('student', 'weekday', 'period'), name='planning_unique_school_slot'),
        ),
        migrations.AddConstraint(
            model_name='studentschoolslot',
            constraint=models.CheckConstraint(condition=models.Q(('period__gte', 1), ('period__lte', 12), _connector='AND'), name='planning_school_slot_period_range'),
        ),
    ]
