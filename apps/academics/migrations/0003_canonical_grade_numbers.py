from django.db import migrations


def populate(apps, schema_editor):
    Grade = apps.get_model('academics', 'Grade')
    # Only unambiguous canonical names; unknown grades need explicit admin metadata.
    names = ['اول', 'دوم', 'سوم', 'چهارم', 'پنجم', 'ششم', 'هفتم', 'هشتم', 'نهم', 'دهم', 'یازدهم', 'دوازدهم']
    for number, name in enumerate(names, 1):
        if not Grade.objects.filter(number=number).exists():
            Grade.objects.filter(name=name, number__isnull=True).update(number=number)


class Migration(migrations.Migration):
    dependencies = [('academics', '0002_grade_number')]
    operations = [migrations.RunPython(populate, migrations.RunPython.noop)]
