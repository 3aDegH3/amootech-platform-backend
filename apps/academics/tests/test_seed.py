from django.core.management import call_command
from django.test import TestCase

from apps.academics.models import Chapter, Field, Grade, Subject, Topic


class AcademicSeedTests(TestCase):
    def test_seed_populates_each_grade_and_field_and_is_idempotent(self):
        call_command("seed_academics", verbosity=0)
        counts = tuple(model.objects.count() for model in (Grade, Field, Subject, Chapter, Topic))
        self.assertEqual(Grade.objects.count(), 3)
        self.assertEqual(Field.objects.count(), 9)
        for grade in Grade.objects.all():
            for field in grade.fields.all():
                self.assertGreaterEqual(field.subjects.count(), 8)
                for subject in field.subjects.all():
                    self.assertGreaterEqual(subject.chapters.count(), 2)
                    self.assertTrue(all(chapter.topics.count() >= 2 for chapter in subject.chapters.all()))
        call_command("seed_academics", verbosity=0)
        self.assertEqual(counts, tuple(model.objects.count() for model in (Grade, Field, Subject, Chapter, Topic)))
