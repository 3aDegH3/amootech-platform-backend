# Generated for fixed schedule v2: slot times + dated events
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_studentprofile_school_name'),
        ('planning', '0006_add_fixed_schedule'),
    ]

    operations = [
        migrations.AddField(
            model_name='studentschoolslot',
            name='start_time',
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='studentschoolslot',
            name='end_time',
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='StudentFixedEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('date', models.DateField()),
                ('start_time', models.TimeField()),
                ('end_time', models.TimeField()),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fixed_events', to='accounts.studentprofile')),
            ],
            options={
                'ordering': ('date', 'start_time', 'pk'),
            },
        ),
    ]
